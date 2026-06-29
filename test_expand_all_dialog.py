"""End-to-end regression for the Expand-All *dialog* flow.

Spins up a real freeopcua server and drives the live ``Window`` via the
``actionExpandAll`` action — not the bare ``TreeWidget.expand_all_async``
path that the BFS worker test exercises. We want to assert that:

- The ``QProgressDialog`` is created and shown immediately on click (the
  "弹框未立刻弹出" bug).
- The dialog is closed once the worker reports ``expand_completed("ok")``
  (the "弹框扫描完会卡住" bug).
- The action button is re-enabled after completion (so a second click is
  accepted).

Runs under ``QT_QPA_PLATFORM=offscreen`` so it doesn't need a display.
"""

from __future__ import annotations

import sys
import time
import unittest

from asyncua.sync import Server
from PyQt6.QtCore import QEventLoop
from PyQt6.QtWidgets import QApplication, QProgressDialog

from uaclient.mainwindow import Window


_URL = "opc.tcp://localhost:48412/freeopcua/server/"


def _pump(seconds: float) -> None:
    app = QApplication.instance()
    assert app is not None
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)


def _pump_until(predicate, timeout: float) -> bool:
    app = QApplication.instance()
    assert app is not None
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.02)
    return predicate()


class TestExpandAllDialog(unittest.TestCase):
    def setUp(self) -> None:
        self.server = Server()
        self.server.set_endpoint(_URL)
        self.server.start()
        self.window = Window()
        self.window.ui.addrComboBox.setCurrentText(_URL)
        self.window.connect()
        # Park on the Objects folder, which is the only Object node
        # by default the model installs at connect time.
        objects = self.server.nodes.objects
        self.window.tree_ui.set_root_node(objects)
        source_idx = self.window.tree_ui.model.index_of_node(objects)
        assert source_idx is not None
        proxy_idx = self.window.tree_ui._source_to_proxy(source_idx)  # type: ignore[attr-defined]
        self.window.tree_ui.view.setCurrentIndex(proxy_idx)

    def tearDown(self) -> None:
        _pump(0.5)
        if self.window.tree_ui.is_expanding():
            self.window.tree_ui.cancel_expand()
            _pump(0.5)
        self.window.disconnect()
        try:
            self.server.stop()
        except Exception:
            pass

    def test_dialog_appears_immediately_and_closes_on_completion(self) -> None:
        # No dialog before the click.
        self.assertIsNone(self.window._expand_progress_dialog)

        # Trigger the action the same way the user would.
        self.window.ui.actionExpandAll.trigger()

        # The dialog must be live right after the slot returns — not
        # several seconds later when the first worker batch lands. The
        # previous bug had QProgressDialog with setMinimumDuration(0)
        # + setRange(0, 0) which Qt refuses to show until a value
        # changes; the fix calls show() explicitly.
        dialog = self.window._expand_progress_dialog
        self.assertIsInstance(dialog, QProgressDialog)
        self.assertTrue(dialog.isVisible(), "dialog should be visible immediately after click")

        # Drain the event loop until the worker reports done. The full
        # BFS walk over the default freeopcua address space takes a
        # few seconds; 30s is plenty.
        self.assertTrue(
            _pump_until(lambda: not self.window.tree_ui.is_expanding(), timeout=30.0),
            "expand never completed",
        )
        # Let the queued expand_completed signal land so the dialog
        # is actually closed.
        self.assertTrue(
            _pump_until(lambda: self.window._expand_progress_dialog is None, timeout=5.0),
            "dialog was not closed after expand_completed",
        )
        self.assertIsNone(self.window._expand_progress_dialog)
        # The action must be re-enabled so a second click is accepted.
        self.assertTrue(self.window.ui.actionExpandAll.isEnabled())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    unittest.main()
