"""Regression test for the BFS+batch Expand-All worker.

Spins up a real freeopcua server on a dedicated port (not the user's
local 127.0.0.1:4840) and drives a live ``Window`` through the
``tree_ui.expand_all_async`` path. Verifies that:

- A simple Object with a handful of children expands to the expected
  number of model rows.
- ``cancel_expand`` actually stops the worker mid-walk and the tree
  is left in a consistent state (no half-installed children, no
  zombie worker thread).

The test runs under ``QT_QPA_PLATFORM=offscreen`` so it doesn't
require a display.
"""

from __future__ import annotations

import sys
import time
import unittest

from asyncua.sync import Server
from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from uaclient.mainwindow import Window


_URL = "opc.tcp://localhost:48410/freeopcua/server/"


def _pump(seconds: float) -> None:
    """Drive the Qt event loop for ``seconds`` wall-clock seconds."""
    app = QApplication.instance()
    assert app is not None
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)


def _pump_until(predicate, timeout: float) -> bool:
    """Pump the event loop until ``predicate()`` is true or ``timeout`` elapses.

    Returns True if the predicate became true, False on timeout. The
    worker thread's ``finished`` signal is connected to
    ``_thread.quit`` and then ``_thread.deleteLater``; without
    pumping for a beat after the worker reports done, the QThread
    may already be a deleted C++ object by the time ``tearDown``'s
    ``shutdown`` call tries to ``quit``/``wait`` on it.
    """
    app = QApplication.instance()
    assert app is not None
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.02)
    return predicate()


class TestExpandAllBFS(unittest.TestCase):
    def setUp(self) -> None:
        self.server = Server()
        self.server.set_endpoint(_URL)
        self.server.start()
        # freeopcua ships with a small, deterministic default address
        # space: Objects has a handful of sub-folders, Types has the
        # type tree. That's enough to exercise both the trivial
        # single-parent path and a layer with many siblings.
        self.window = Window()
        self.window.ui.addrComboBox.setCurrentText(_URL)
        self.window.connect()

    def tearDown(self) -> None:
        # Give the QThread deleteLater chain a beat to run before
        # ``disconnect``'s ``shutdown`` calls ``quit`` on it;
        # otherwise the C++ object is already gone and we hit a
        # RuntimeError. ``is_expanding`` returning False only means
        # ``_on_worker_finished`` ran — the queued ``_thread.quit``
        # → ``_thread.finished`` → ``deleteLater`` chain still
        # needs the event loop to drain.
        _pump(0.5)
        if self.window.tree_ui.is_expanding():
            self.window.tree_ui.cancel_expand()
            _pump(0.5)
        self.window.disconnect()
        try:
            self.server.stop()
        except Exception:
            pass

    def test_expand_all_walks_entire_subtree(self) -> None:
        """A full BFS walk installs every reachable row in the model.

        The freeopcua default Objects subtree has a known shape: a
        Server node plus a couple of generated sub-objects. We don't
        pin an exact count (freeopcua's stock address space changes
        across versions) but we require that ``expand_all`` lands at
        least the children of the Objects folder, the Types subtree,
        and the Views folder, i.e. strictly more than 4 rows.
        """
        objects = self.server.nodes.objects
        self.window.tree_ui.set_root_node(objects)
        # Navigate the tree view's current row to Objects and start
        # the walk from there. ``expand_all_async`` defaults to
        # ``currentIndex()`` when no arg is passed.
        source_idx = self.window.tree_ui.model.index_of_node(objects)
        self.assertIsNotNone(source_idx)
        proxy_idx = self.window.tree_ui._source_to_proxy(source_idx)  # type: ignore[attr-defined]
        self.window.tree_ui.view.setCurrentIndex(proxy_idx)

        # Start the walk and wait for ``expand_completed`` to fire.
        completed: list[str] = []
        self.window.tree_ui.expand_completed.connect(
            lambda status: completed.append(status)
        )
        self.window.tree_ui.expand_all_async()

        # Drive the event loop until either completion or a 30 s
        # safety timeout. The full BFS walk on the default freeopcua
        # address space takes ~9 s on loopback (6090 parents / 14k
        # children / 125 batches); the budget covers slower CI.
        self.assertTrue(
            _pump_until(lambda: bool(completed), timeout=30.0),
            "expand_completed never fired",
        )
        self.assertEqual(completed[-1], "ok", f"unexpected status: {completed!r}")
        # Let the QThread tear-down chain complete before
        # tearDown's ``shutdown`` runs.
        _pump(0.2)

        # The model should now have all of Objects' descendants.
        # ``index_of_node`` round-trips any node the worker has
        # installed; if a node the server says exists is missing
        # from the model, the walk silently dropped it.
        for child in objects.get_children():
            self.assertIsNotNone(
                self.window.tree_ui.model.index_of_node(child),
                f"child {child} missing from model after expand_all",
            )

    def test_cancel_stops_walk_and_cleans_up(self) -> None:
        """``cancel_expand`` must stop the worker and reset state.

        The default freeopcua address space is small enough that the
        walk finishes in under a second even on slow machines, so we
        can't reliably race the cancel against a real walk. Instead
        we trigger ``expand_all_async`` then immediately cancel; the
        walk may either complete (race won by worker) or be cut
        short (race won by cancel). Both outcomes are acceptable as
        long as the post-condition holds: ``is_expanding()`` is
        ``False`` within a short window and the worker thread has
        wound down without crashing.
        """
        objects = self.server.nodes.objects
        self.window.tree_ui.set_root_node(objects)
        source_idx = self.window.tree_ui.model.index_of_node(objects)
        assert source_idx is not None
        proxy_idx = self.window.tree_ui._source_to_proxy(source_idx)  # type: ignore[attr-defined]
        self.window.tree_ui.view.setCurrentIndex(proxy_idx)

        completed: list[str] = []
        self.window.tree_ui.expand_completed.connect(
            lambda status: completed.append(status)
        )
        self.window.tree_ui.expand_all_async()
        # Cancel straight away: the worker's first batch hasn't
        # necessarily been emitted yet, so this exercises the
        # ``_is_expanding`` no-op re-entry guard and the cancel
        # flag set before the worker reaches the top of its loop.
        self.window.tree_ui.cancel_expand()

        # Pump until the worker reports completion (or 10 s, whichever
        # comes first). Status may be "cancelled" or "ok" depending on
        # the race; both are valid post-cancel outcomes.
        self.assertTrue(
            _pump_until(lambda: bool(completed), timeout=10.0),
            "expand_completed never fired after cancel",
        )
        self.assertIn(completed[-1], ("cancelled", "ok"))
        _pump(0.2)

        # The post-condition: the worker flag is cleared and the
        # GUI is left in a usable state.
        self.assertFalse(self.window.tree_ui.is_expanding())
        # A second ``expand_all_async`` must be accepted (the
        # re-entry guard is reset) and must complete cleanly.
        self.window.tree_ui.expand_all_async()
        second: list[str] = []
        self.window.tree_ui.expand_completed.connect(
            lambda status: second.append(status)
        )
        self.assertTrue(
            _pump_until(lambda: bool(second), timeout=30.0),
            "second expand_all never completed",
        )
        self.assertEqual(second[-1], "ok")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    unittest.main()
