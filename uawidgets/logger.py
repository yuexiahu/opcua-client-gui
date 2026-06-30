import logging

from PyQt6.QtCore import Q_ARG, QMetaObject, Qt
from PyQt6.QtWidgets import QTextEdit


class QtHandler(logging.Handler):

    def __init__(self, widget: QTextEdit) -> None:
        logging.Handler.__init__(self)
        self.setFormatter(logging.Formatter("%(name)s - %(levelname)s - %(message)s"))
        self.widget = widget

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        # QTextEdit.append is a UI method and is only safe to call
        # from the GUI thread. The expand-all worker (and any other
        # background thread that calls ``logger.warning`` /
        # ``logger.exception``) will land here off-thread, and a
        # direct ``self.widget.append`` from a worker thread races
        # the GUI thread's own append calls — on Windows that
        # surfaces as a hard crash right after the red status bar
        # appears, with no Python traceback (Qt widget internals
        # segfault). Marshal the call back to the widget's thread
        # via a queued ``invokeMethod`` so the actual append runs
        # on the GUI thread's event loop. Returns immediately on
        # the caller.
        QMetaObject.invokeMethod(
            self.widget,
            "append",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, msg),
        )
