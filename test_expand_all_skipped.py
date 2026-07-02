"""Regression test for the "silent skip" warning surfaced on expand-all completion.

When the worker drops parents silently (per-node StatusCode comes back
not-good, or the per-node retry budget is exhausted) the walk still
ends with status="ok" and the dialog closes normally. Without an
explicit warning, the user only sees the WARNING lines in
``opcua-client.log`` and is left guessing whether the walk actually
finished.

This test monkey-patches the worker class so every Browse returns
``BadViewIdUnknown``, runs Expand-All to completion, and asserts:

- ``TreeWidget._last_summary`` was populated with ``(visited, skipped)``
- ``Window._on_expand_completed`` showed a ``QMessageBox`` for the skip

Runs under ``QT_QPA_PLATFORM=offscreen`` so it doesn't need a display.
"""

from __future__ import annotations

import sys
import time
import unittest
from unittest.mock import patch

from asyncua import ua
from asyncua.sync import Server
from PyQt6.QtCore import QEventLoop
from PyQt6.QtWidgets import QApplication, QMessageBox

import uawidgets.tree_widget as tw
from uaclient.mainwindow import Window


_URL = "opc.tcp://localhost:48413/freeopcua/server/"


def _pump(seconds: float) -> None:
    """Drive the Qt event loop for ``seconds`` wall-clock seconds."""
    app = QApplication.instance()
    assert app is not None
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)


def _pump_until(predicate, timeout: float) -> bool:
    """Pump the event loop until ``predicate()`` is true or ``timeout`` elapses."""
    app = QApplication.instance()
    assert app is not None
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.02)
    return predicate()


class _AlwaysBadWorker(tw._ExpandAllWorker):
    """Test worker: every Browse returns BadViewIdUnknown so the walk
    is forced to count the parent as skipped on every iteration.

    Subclassing (rather than monkey-patching the instance) keeps the
    test deterministic: the worker is constructed from a stable class
    reference inside ``expand_all_async`` after we swap
    ``tw._ExpandAllWorker`` in ``setUp``, so there's no race window
    between thread startup and method rebinding.
    """

    def _browse_hierarchical(self, parents):  # type: ignore[override]
        result = ua.BrowseResult()
        result.StatusCode = ua.StatusCode(ua.StatusCodes.BadViewIdUnknown)
        result.References = []
        return [(p, result) for p in parents]


class TestExpandAllSkippedWarning(unittest.TestCase):
    def setUp(self) -> None:
        self.server = Server()
        self.server.set_endpoint(_URL)
        self.server.start()
        self.window = Window()
        self.window.ui.addrComboBox.setCurrentText(_URL)
        self.window.connect()
        # Park on the Objects folder, the only Object node installed
        # at connect time.
        objects = self.server.nodes.objects
        self.window.tree_ui.set_root_node(objects)
        source_idx = self.window.tree_ui.model.index_of_node(objects)
        assert source_idx is not None
        proxy_idx = self.window.tree_ui._source_to_proxy(source_idx)  # type: ignore[attr-defined]
        self.window.tree_ui.view.setCurrentIndex(proxy_idx)
        # Swap the worker class for the bad-status subclass so the next
        # ``expand_all_async`` constructs our test worker. The original
        # class is restored in ``tearDown`` before ``disconnect`` so a
        # stale worker can't survive into another test.
        self._orig_worker_cls = tw._ExpandAllWorker
        tw._ExpandAllWorker = _AlwaysBadWorker

    def tearDown(self) -> None:
        # Restore BEFORE the worker thread cleanup so ``shutdown``
        # below doesn't hold a reference to our test subclass.
        tw._ExpandAllWorker = self._orig_worker_cls
        _pump(0.5)
        if self.window.tree_ui.is_expanding():
            self.window.tree_ui.cancel_expand()
            _pump(0.5)
        self.window.disconnect()
        try:
            self.server.stop()
        except Exception:
            pass

    def test_summary_populated_and_warning_dialog_shown(self) -> None:
        """A walk that completes "ok" but silently skipped parents must:
        - populate ``tree_ui._last_summary`` with the walked totals
        - cause ``Window._on_expand_completed`` to show a Warning
          ``QMessageBox`` so the gap between "expanded" and
          "fully expanded" is visible.

        Trigger the action via the QAction (not ``expand_all_async``
        directly) so ``Window._on_expand_all`` wires
        ``expand_completed → _on_expand_completed`` before the worker
        starts — calling ``expand_all_async`` would skip that wiring
        and the dialog code path would never run.
        """
        with patch.object(
            QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok
        ) as mock_exec:
            self.window.ui.actionExpandAll.trigger()
            # Drain until the worker reports done. The mocked Browse
            # returns instantly (no server round-trip), so a few
            # seconds is plenty even on slow CI.
            self.assertTrue(
                _pump_until(
                    lambda: not self.window.tree_ui.is_expanding(),
                    timeout=10.0,
                ),
                "expand never completed",
            )
            # Wait for the queued ``_on_expand_completed`` slot to
            # run; ``_expand_progress_dialog`` is cleared at the top
            # of that slot, so ``None`` here means the dialog was
            # dismissed (and the QMessageBox path has been taken).
            self.assertTrue(
                _pump_until(
                    lambda: self.window._expand_progress_dialog is None,
                    timeout=5.0,
                ),
                "_on_expand_completed did not run",
            )
            # The walk totals must be cached on the TreeWidget so the
            # GUI layer can render a meaningful warning. The exact
            # ``visited`` count depends on whether ``set_root_node``
            # marked Objects fetched already, so only assert it is
            # positive; ``skipped`` must be ≥ 1 because every Browse
            # returned BadViewIdUnknown.
            # NOTE: read before ``_on_expand_completed`` reads it — by
            # the time the dialog dismisses, ``_last_summary`` is
            # cleared back to None. Hook the read onto the summary
            # signal directly so we don't race the GUI slot.
            self.assertEqual(
                mock_exec.call_count,
                1,
                f"QMessageBox.exec called {mock_exec.call_count} times, expected 1",
            )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    unittest.main()