import csv
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from asyncua.sync import Client as SyncClient, sync_wrapper
from asyncua.client.ua_client import UaClient as _AsyncUaClient, UaClientState
from asyncua.ua import BrowseNextParameters, BrowseParameters, BrowseDescription, BrowseDirection, BrowseResultMask, ObjectIds, NodeClass

from PyQt6.QtCore import (
    pyqtSignal,
    pyqtSlot,
    QMimeData,
    QModelIndex,
    QObject,
    QSize,
    Qt,
    QSettings,
    QSortFilterProxyModel,
    QThread,
    QAbstractItemModel,
)
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QIcon, QAction
from PyQt6.QtWidgets import QApplication, QAbstractItemView, QFileDialog, QHeaderView, QLineEdit, QTreeView

from asyncua import ua
from asyncua.sync import SyncNode, new_node


logger = logging.getLogger(__name__)

# Back to v3: an earlier iteration added a 4th 'Path' column to the
# model and bumped the key forward; the user wanted the path to be
# computed at export time instead, so the model is back to 3 columns
# and the saved header state from v4/v5 (which referenced the extra
# column) is dropped. The v3 key was the original — restoring it
# means any pre-v4 install gets the original behaviour back too.
_HEADER_STATE_KEY = "tree_widget_state_v3"

# Custom role on the column-0 QStandardItem. Set to ``True``/``False``
# from ``desc.NodeClass`` at insert time so ``hasChildren`` can avoid
# drawing a (misleading) expand arrow on leaf node types like
# ``Variable`` or ``Method`` without doing a server round-trip per row.
_CAN_HAVE_CHILDREN_ROLE = Qt.ItemDataRole.UserRole + 1

# Stores the ua.NodeClass enum int on the column-0 item. NodeClass
# is not shown in the tree but the CSV export wants it; caching the
# int here avoids a ``read_node_class`` server round-trip per row
# during export. The role is set once at insert time and read rarely,
# so the per-row overhead is small. Drop this role (and re-read
# NodeClass at export time) if you'd rather the model carry nothing
# the tree doesn't display.
_NODE_CLASS_ROLE = Qt.ItemDataRole.UserRole + 2


def _node_class_can_have_children(node_class: ua.NodeClass) -> bool:
    # OPC UA spec: only Object / ObjectType / VariableType / View can
    # have hierarchical children. Variables, Methods, DataTypes and
    # ReferenceTypes are always leaves.
    return node_class in (
        ua.NodeClass.Object,
        ua.NodeClass.ObjectType,
        ua.NodeClass.VariableType,
        ua.NodeClass.View,
    )


class TreeFilterProxyModel(QSortFilterProxyModel):
    """Proxy that filters rows by per-column substring matches.

    Each column's filter text is stored on the corresponding QLineEdit. A row
    passes the filter only if every non-empty column filter is a case-insensitive
    substring of that column's DisplayRole data. Rows that don't match are hidden,
    but with ``recursiveFilteringEnabled`` Qt keeps their ancestors visible so
    the tree context is preserved.
    """

    def __init__(self, filter_widgets: list[QLineEdit]) -> None:
        super().__init__()
        self._filter_widgets = filter_widgets
        self.setRecursiveFilteringEnabled(True)
        # Source model is already sorted by BrowseName in _fetchMore; do not
        # re-sort at the proxy layer (would re-order by DisplayName).
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if model is None:
            return True
        for col, edit in enumerate(self._filter_widgets):
            text = edit.text().strip().lower()
            if not text:
                continue
            idx = model.index(source_row, col, source_parent)
            if not idx.isValid():
                return False
            data = idx.data(Qt.ItemDataRole.DisplayRole) or ""
            if text not in str(data).lower():
                return False
        return True


class TreeWidget(QObject):

    error = pyqtSignal(Exception)
    # Forwarded from the expand-all worker so the main window can drive
    # a progress dialog without owning the worker itself.
    expand_progress = pyqtSignal(int, str)  # (visited_count, current_path)
    expand_completed = pyqtSignal(str)  # "ok" | "cancelled" | "error"

    def __init__(self, view: QTreeView, filter_widgets: list[QLineEdit] | None = None) -> None:
        QObject.__init__(self, view)
        self.view = view
        self.model = TreeViewModel()
        self.model.error.connect(self.error)
        # Late-bound: Window calls ``set_client`` after the underlying
        # ``UaClient.connect`` succeeds, since the sync ``Client`` is
        # None until that point. The expand-all worker needs a non-None
        # client to call ``browse_nodes``; ``expand_all_async`` no-ops
        # if it isn't set.
        self._client: SyncClient | None = None

        self.model.setHorizontalHeaderLabels(['DisplayName', "BrowseName", 'NodeId'])
        # Clamp icon rendering size; the bundled SVGs lack viewBox attributes
        # and Qt6's SVG painter logs "buffer size too big" when asked to
        # render them at unbounded sizes.
        self.view.setIconSize(QSize(16, 16))
        header = self.view.header()
        assert header is not None
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        # Optional column-filter row. When provided, sit a QSortFilterProxyModel
        # between the source model and the view so per-column substring
        # filtering and recursive ancestor visibility work out of the box.
        self.proxy: TreeFilterProxyModel | None = None
        if filter_widgets:
            self.proxy = TreeFilterProxyModel(filter_widgets)
            self.proxy.setSourceModel(self.model)
            self.view.setModel(self.proxy)
            for edit in filter_widgets:
                edit.textChanged.connect(self._on_filter_changed)
        else:
            self.view.setModel(self.model)

        self.settings = QSettings()
        state = self.settings.value(_HEADER_STATE_KEY, None)
        if state is not None:
            header.restoreState(state)

        self.actionReload = QAction("Reload", self)
        self.actionReload.triggered.connect(self.reload_current)

        # Expand-all state. The thread + worker pair is created lazily
        # by ``expand_all_async``; ``shutdown`` cancels and joins any
        # in-flight one on application exit.
        self._worker: _ExpandAllWorker | None = None
        self._thread: QThread | None = None
        self._is_expanding: bool = False
        # Final (visited, skipped) counts from the most recent worker
        # run. Populated by ``_on_worker_summary`` from the worker's
        # ``summary`` signal, consumed by ``Window._on_expand_completed``
        # to surface a warning when a "successful" walk silently
        # skipped parts of the tree. Reset on every ``expand_all_async``
        # so a missing summary unambiguously means "no run yet", not
        # "ran and skipped 0".
        self._last_summary: tuple[int, int] | None = None

    def save_state(self) -> None:
        header = self.view.header()
        if header is not None:
            self.settings.setValue(_HEADER_STATE_KEY, header.saveState())

    def set_client(self, client: SyncClient | None) -> None:
        """Bind the sync ``Client`` used by the BFS batch worker.

        Called from ``Window.connect`` after the underlying ``UaClient``
        finishes handshaking, since the sync ``Client`` is ``None`` until
        then. Safe to call again on reconnect to refresh the binding.
        """
        self._client = client

    def clear(self) -> None:
        self.model.clear()

    def set_root_node(self, node: SyncNode) -> None:
        self.model.clear()
        self.model.set_root_node(node)
        idx = self._source_to_proxy(self.model.index(0, 0))
        self.view.expandToDepth(0)
        if idx.isValid():
            self.view.setCurrentIndex(idx)

    def copy_path(self) -> None:
        path = self.get_current_path()
        path_str = ",".join(path)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(path_str)

    def export_csv(self) -> None:
        """Export the current node's subtree to a CSV file.

        Writes one row per node currently in the model under the
        selected row, in DFS order. Columns: Path, DisplayName,
        BrowseName, NodeId, NodeClass. The file is opened with a
        utf-8-sig BOM so Excel imports it cleanly without re-encoding.

        The export covers only what the model already has loaded:
        rows that haven't been browsed yet are not in the model and
        will not appear in the CSV. Use Expand All first if a
        complete subtree is wanted. This deliberately avoids
        piggy-backing on the expand-all worker — that worker can
        take a long time on a server with thousands of nodes (and
        can outright fail on the S7-1500 in Batched mode), and a
        synchronous "auto-expand before export" call would freeze
        the GUI on top of that.

        Filter state is intentionally ignored: the proxy model is
        a browsing aid, the CSV is meant to be a complete dump of
        the selected subtree.
        """
        current = self.view.currentIndex()
        if not current.isValid():
            return
        path, _ = QFileDialog.getSaveFileName(
            self.view,
            "Export Tree to CSV",
            "opcua-tree.csv",
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        # Map the proxy index back to the source so the walk covers
        # the full subtree, not just what the filter happens to
        # admit. ``sibling(row, 0)`` normalises to the column-0 row
        # anchor (everything else rides on that).
        if self.proxy is not None:
            start = self.proxy.mapToSource(current.sibling(current.row(), 0))
        else:
            start = current.sibling(current.row(), 0)
        try:
            # utf-8-sig BOM so Excel reads the file as UTF-8 without
            # the user having to import. ``newline=""`` lets csv.writer
            # own the line terminator; without it, on Windows Python
            # would emit \r\r\n between rows.
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    ["Path", "DisplayName", "BrowseName", "NodeId", "NodeClass"]
                )
                self._walk_csv_to(start, self.model, writer)
        except OSError as ex:
            self.error.emit(ex)

    def _walk_csv_to(
        self,
        idx: QModelIndex,
        model: QAbstractItemModel,
        writer: Any,
    ) -> None:
        """DFS walk writing one CSV row per index.

        Pulls each column's DisplayRole text directly from the
        source model so the CSV matches what the user sees in the
        tree. The path is computed on the fly (see ``_path_for``)
        rather than read from a column — the model carries no Path
        data by design. NodeClass comes from a role on the
        column-0 item, since it isn't a visible column either.
        """
        if not idx.isValid():
            return
        row = idx.row()
        parent = idx.parent()
        path = self._path_for(idx)
        dname = model.data(model.index(row, 0, parent)) or ""
        bname = model.data(model.index(row, 1, parent)) or ""
        nodeid = model.data(model.index(row, 2, parent)) or ""
        node_class_int = model.data(idx, _NODE_CLASS_ROLE)
        # The role was stored as int; reconstructing the enum lets
        # us render ``Object`` rather than ``1`` in the CSV without
        # hard-coding the mapping here.
        node_class_str = (
            ua.NodeClass(node_class_int).name
            if node_class_int is not None
            else ""
        )
        writer.writerow([path, dname, bname, nodeid, node_class_str])
        for r in range(model.rowCount(idx)):
            child = model.index(r, 0, idx)
            if child.isValid():
                self._walk_csv_to(child, model, writer)

    def _path_for(self, idx: QModelIndex) -> str:
        """Build the "/"-joined BrowseName path for ``idx``.

        Walks ``idx.parent()`` up to the root, reading each row's
        column-1 (BrowseName) text. The text is populated by
        ``TreeViewModel.add_item`` from the ReferenceDescription
        the server returned at Browse time, so this is a pure
        in-memory walk with no server round-trips. The result is
        the same shape the user originally asked for:
        ``"Root/Objects/Server"``.

        Computing on demand (instead of caching on the model) keeps
        the model free of fields the tree view doesn't render; the
        walk is O(depth) per row, which for an OPC-UA tree is
        typically a handful of levels, and ``_walk_csv_to`` visits
        each row exactly once.
        """
        parts: list[str] = []
        cur = idx
        while cur.isValid():
            bname = self.model.data(cur.sibling(cur.row(), 1)) or ""
            if bname:
                parts.append(bname)
            cur = cur.parent()
        return "/".join(reversed(parts))

    def expand_current_node(self, expand: bool = True) -> None:
        idx = self.view.currentIndex()
        self.view.setExpanded(idx, expand)

    def expand_all_async(
        self,
        idx: QModelIndex | None = None,
        mode: str = "fast",
    ) -> None:
        """Recursively expand the row at ``idx`` (or the current row) and
        all of its descendants, off the GUI thread.

        ``mode`` is either ``"fast"`` (BFS, up to 50 parents per Browse
        call) or ``"normal"`` (DFS, one parent per Browse call — the
        original on-demand semantics, useful as a fallback on servers
        that reject batched Browse).

        Already-fetched subtrees are skipped: ``TreeViewModel.unexpanded_descendants``
        walks the model on the GUI thread and returns every node whose
        children are not yet installed. Those nodes become the worker's
        starting queue, so a retry after a partial walk (e.g. a
        ``BadNoContinuationPoints`` failure partway down the tree) only
        re-browses the boundary between "expanded" and "not yet
        expanded" rather than starting from the root again.

        The OPC-UA I/O for each visited node happens in a QThread worker
        (``_ExpandAllWorker``); the GUI side only installs the resulting
        rows into the model and toggles ``setExpanded`` between events.
        Progress and completion are forwarded via the ``expand_progress``
        and ``expand_completed`` signals.

        Re-entry while an expand is already running is a silent no-op;
        the caller (``Window._on_expand_all``) is also expected to
        disable the triggering action for the duration.
        """
        if self._is_expanding:
            return
        # Drop any summary left over from the previous run. Reset
        # unconditionally on entry (not just before worker startup)
        # so an early-return path like "no client bound" or "nothing
        # to expand" leaves ``_last_summary`` as ``None``; that way
        # ``_on_expand_completed`` can treat "summary missing" as
        # "no walk totals reported" rather than reading a stale
        # ``(visited, 0)`` from a prior clean run.
        self._last_summary = None
        if self._client is None:
            # BFS batch worker needs the sync ``Client``; if Window
            # forgot to call ``set_client`` after connect, we silently
            # give up rather than crash on a None dereference inside
            # the worker.
            logger.error("expand_all_async: no client bound; was set_client() called?")
            self.expand_completed.emit("error")
            return
        # QAction.triggered hands us a bool (the checked state); ignore
        # it and fall back to the current row.
        if not isinstance(idx, QModelIndex):
            idx = self.view.currentIndex()
        if not idx.isValid():
            return
        # UserRole data (and child navigation) live in column 0. Normalize
        # so canFetchMore/fetchMore don't read UserRole from the wrong cell.
        idx = idx.sibling(idx.row(), 0)
        source_idx = self._proxy_to_source(idx)
        item = self.model.itemFromIndex(source_idx)
        if item is None:
            return
        node = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(node, SyncNode):
            return
        # Pick the worker's starting nodes on the GUI thread. On a
        # fresh tree this returns ``[node]``; on a retry it returns
        # the first un-expanded node in every partially-expanded
        # branch, so the worker only re-browses what's actually
        # missing. The worker's own ``_browsed`` set still dedups
        # any overlap between the seed list and what gets discovered
        # during the walk.
        start_nodes = self.model.unexpanded_descendants(source_idx)
        if not start_nodes:
            # Everything is already expanded. Avoid spinning up a
            # worker for an empty queue; just notify the caller of
            # a no-op completion.
            logger.info(
                "expand_all_async: nothing to expand under %s; "
                "skipping worker startup",
                node.nodeid,
            )
            self.expand_completed.emit("ok")
            return
        # Spin up a fresh worker + thread per call; auto-cleanup on
        # completion. ~10 ms spin-up cost is dwarfed by server RTT.
        self._worker = _ExpandAllWorker(self._client, mode=mode)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.fetch_requested.connect(self._on_fetch_requested, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        self._worker.progress.connect(self.expand_progress, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        self._worker.summary.connect(self._on_worker_summary, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        self._worker.finished.connect(self._on_worker_finished, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        self._worker.failed.connect(self._on_worker_failed, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        # Tear-down chain: finished → stop thread; thread done → free.
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._worker.start(start_nodes)
        self._is_expanding = True
        self._thread.start()

    def cancel_expand(self) -> None:
        """Request cancellation of the running expand (if any)."""
        if self._worker is not None:
            self._worker.cancel()

    def is_expanding(self) -> bool:
        return self._is_expanding

    def shutdown(self) -> None:
        """Stop the worker thread, if any. Called from
        :meth:`Window.closeEvent` and from :meth:`Window.disconnect`.
        Idempotent.
        """
        if self._thread is None:
            return
        if self._worker is not None:
            self._worker.cancel()
        thread = self._thread
        self._thread = None
        self._worker = None
        try:
            thread.quit()
        except RuntimeError:
            # The worker already finished and the ``thread.finished``
            # → ``_thread.deleteLater`` chain already ran. Nothing to
            # stop.
            return
        # Bounded wait so a wedged worker can't hang app shutdown.
        thread.wait(2000)

    @pyqtSlot(object)
    def _on_fetch_requested(self, payload: object) -> None:
        """GUI-side handler: install one BFS layer's children and expand them."""
        if not isinstance(payload, _FetchBatch):
            return
        if self._worker is not None and self._worker._cancelled:
            # Cancel landed mid-flight; drop the batch and let the worker's
            # ``finished("cancelled")`` tear things down.
            return
        for parent_node, descs in payload.groups:
            try:
                self.model.add_fetched_children(parent_node, descs)
            except KeyError:
                # Disconnect / clear raced with the worker. Nothing to
                # install for this parent; keep going for the rest of
                # the layer in case their rows are still valid.
                continue
            # Expand the parent now that the children are in the model
            # so the user sees the tree fill in. In BFS the parent
            # may already be expanded (it had to be to be in the
            # worker's batch at all), but ``setExpanded`` is idempotent.
            source_idx = self.model.index_of_node(parent_node)
            if source_idx is None:
                continue
            proxy_idx = self._source_to_proxy(source_idx)
            if proxy_idx.isValid():
                self.view.setExpanded(proxy_idx, True)

    @pyqtSlot(int, int)
    def _on_worker_summary(self, visited: int, skipped: int) -> None:
        # Cached so ``Window._on_expand_completed`` can read the final
        # walk totals when ``finished`` arrives. ``summary`` is emitted
        # *before* ``finished`` so the QueuedConnection ordering
        # guarantees this slot has run by the time the completion
        # handler looks at ``_last_summary``.
        self._last_summary = (visited, skipped)

    @pyqtSlot(str)
    def _on_worker_finished(self, status: str) -> None:
        self._is_expanding = False
        self.expand_completed.emit(status)

    @pyqtSlot(object)
    def _on_worker_failed(self, ex: object) -> None:
        if isinstance(ex, BaseException):
            self.error.emit(ex)

    def expand_to_node(self, node: SyncNode | str) -> None:
        """
        Expand tree until given node and select it
        """
        if isinstance(node, str):
            idxlist = self.model.match(self.model.index(0, 0), Qt.ItemDataRole.DisplayRole, node, 1, Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive)
            if not idxlist:
                raise ValueError(f"Node {node} not found in tree")
            node = self.model.data(idxlist[0], Qt.ItemDataRole.UserRole)
        path = node.get_path()
        for path_node in path:
            try:
                text = path_node.read_display_name().Text
            except ua.UaError:
                return
            idxlist = self.model.match(self.model.index(0, 0), Qt.ItemDataRole.DisplayRole, text, 1, Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive)
            if idxlist:
                proxy_idx = self._source_to_proxy(idxlist[0])
                if not proxy_idx.isValid():
                    return
                self.view.setExpanded(proxy_idx, True)
                self.view.setCurrentIndex(proxy_idx)
                self.view.activated.emit(proxy_idx)
            else:
                logger.warning("While expanding tree, could not find node %s in tree view, this might be OK", path_node)

    def copy_nodeid(self) -> None:
        node = self.get_current_node()
        if node is None:
            return
        text = node.nodeid.to_string()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def get_current_path(self) -> list[str]:
        idx = self._proxy_to_source(self.view.currentIndex())
        idx = idx.sibling(idx.row(), 0)
        it: QStandardItem | None = self.model.itemFromIndex(idx)
        path: list[str] = []
        while it and it.data(Qt.ItemDataRole.UserRole):
            node = it.data(Qt.ItemDataRole.UserRole)
            name = node.read_browse_name().to_string()
            path.insert(0, name)
            it = it.parent()
        return path

    def update_browse_name_current_item(self, bname: ua.QualifiedName) -> None:
        idx = self._proxy_to_source(self.view.currentIndex())
        idx = idx.sibling(idx.row(), 1)
        it = self.model.itemFromIndex(idx)
        if it is not None:
            it.setText(bname.to_string())

    def update_display_name_current_item(self, dname: ua.LocalizedText) -> None:
        idx = self._proxy_to_source(self.view.currentIndex())
        idx = idx.sibling(idx.row(), 0)
        it = self.model.itemFromIndex(idx)
        if it is not None:
            it.setText(dname.Text)

    def reload_current(self) -> None:
        idx = self._proxy_to_source(self.view.currentIndex())
        idx = idx.sibling(idx.row(), 0)
        it = self.model.itemFromIndex(idx)
        if not it:
            return
        self.reload(it)

    def reload(self, item: QStandardItem | None = None) -> None:
        if item is None:
            item = self.model.item(0, 0)
        if item is None:
            return
        for _ in range(item.rowCount()):
            child_it = item.child(0, 0)
            if child_it is None:
                continue
            node = child_it.data(Qt.ItemDataRole.UserRole)
            if node:
                self.model.reset_cache(node)
            item.takeRow(0)
        node = item.data(Qt.ItemDataRole.UserRole)
        if node:
            self.model.reset_cache(node)
            self.model.indexFromItem(item)

    def remove_current_item(self) -> None:
        idx = self._proxy_to_source(self.view.currentIndex())
        self.model.removeRow(idx.row(), idx.parent())

    def get_current_node(self, idx: QModelIndex | None = None) -> SyncNode | None:
        if idx is None:
            idx = self.view.currentIndex()
        source_idx = self._proxy_to_source(idx).sibling(idx.row(), 0)
        it = self.model.itemFromIndex(source_idx)
        if not it:
            return None
        node = it.data(Qt.ItemDataRole.UserRole)
        if not node:
            ex = RuntimeError("Item does not contain node data, report!")
            self.error.emit(ex)
            raise ex
        return node

    def get_parent_node(self, idx: QModelIndex | None = None) -> SyncNode | None:
        """Return the parent SyncNode of the row at ``idx`` (or the current row).

        Used by callers that need the containing Object of a selected Method
        node (e.g. the call-method dialog).
        """
        if idx is None:
            idx = self.view.currentIndex()
        if not idx.isValid():
            return None
        source_idx = self._proxy_to_source(idx).sibling(idx.row(), 0)
        it = self.model.itemFromIndex(source_idx)
        if it is None:
            return None
        parent_it = it.parent()
        if parent_it is None:
            return None
        return parent_it.data(Qt.ItemDataRole.UserRole)

    # ------------------------------------------------------------------
    # Index mapping helpers. The view may sit behind a QSortFilterProxyModel,
    # so any index returned from the view needs to be mapped back to the
    # source QStandardItemModel before calling itemFromIndex, and any source
    # index passed to the view (setExpanded, setCurrentIndex, ...) needs to
    # be mapped forward.
    # ------------------------------------------------------------------
    def _proxy_to_source(self, proxy_idx: QModelIndex) -> QModelIndex:
        if self.proxy is None or not proxy_idx.isValid():
            return proxy_idx
        return self.proxy.mapToSource(proxy_idx)

    def _source_to_proxy(self, source_idx: QModelIndex) -> QModelIndex:
        if self.proxy is None or not source_idx.isValid():
            return source_idx
        return self.proxy.mapFromSource(source_idx)

    def _on_filter_changed(self, _text: str) -> None:
        if self.proxy is None:
            return
        self.proxy.invalidateFilter()
        # Auto-expand every visible proxy row so the user actually sees the
        # matching content (Qt's recursive filtering keeps ancestors visible
        # but does not expand them).
        self._expand_visible(self.proxy.index(0, 0))

    def _expand_visible(self, proxy_idx: QModelIndex) -> None:
        model = self.proxy
        if model is None:
            return
        # Walk all rows from ``proxy_idx`` down through its descendants.
        # rowCount() reflects filtered children only, which is what we want.
        for row in range(model.rowCount(proxy_idx)):
            child = model.index(row, 0, proxy_idx)
            if not child.isValid():
                continue
            # Skip rows whose children have not been fetched yet:
            # ``QTreeView.setExpanded`` on such a row synchronously calls
            # ``canFetchMore``/``fetchMore`` on the source model, and our
            # ``fetchMore`` resolves to ``node.get_children_descriptions``
            # — a blocking OPC-UA round-trip. Firing it from inside the
            # filter slot stalls the GUI thread for as long as the server
            # takes to answer. The recursive filter already keeps the
            # row visible, so the user can expand it manually.
            source_child = model.mapToSource(child)
            if self.model.canFetchMore(source_child):
                continue
            self.view.setExpanded(child, True)
            if model.hasChildren(child):
                self._expand_visible(child)


class TreeViewModel(QStandardItemModel):

    error = pyqtSignal(Exception)

    def __init__(self) -> None:
        super().__init__()
        self._fetched: list[SyncNode] = []
        # nodeid.to_string() -> QStandardItem, so the GUI-side expand
        # handler can resolve a row for a SyncNode in O(1) without walking
        # the whole tree on every batch the worker emits. Keying on
        # Python ``id(node)`` is wrong: ``new_node`` builds a fresh
        # wrapper every time, so the worker thread's ``SyncNode`` and
        # the GUI thread's ``SyncNode`` for the same logical node are
        # different objects with different ``id()``s. Without NodeId-
        # based lookup, ``index_of_node`` would return None for every
        # batch the worker sends, ``setExpanded`` would never fire,
        # and the user would see the dialog close with the tree still
        # collapsed even though child rows had been added.
        self._node_to_item: dict[str, QStandardItem] = {}

    def clear(self) -> None:
        # remove all rows but not header!!
        self.removeRows(0, self.rowCount())
        self._fetched = []
        self._node_to_item = {}

    def set_root_node(self, node: SyncNode) -> None:
        desc = self._get_node_desc(node)
        self.add_item(desc, node=node)

    def _get_node_desc(self, node: SyncNode) -> ua.ReferenceDescription:
        attrs = node.read_attributes([ua.AttributeIds.DisplayName, ua.AttributeIds.BrowseName, ua.AttributeIds.NodeId, ua.AttributeIds.NodeClass])
        desc = ua.ReferenceDescription()
        desc.DisplayName = attrs[0].Value.Value
        desc.BrowseName = attrs[1].Value.Value
        desc.NodeId = attrs[2].Value.Value
        desc.NodeClass = attrs[3].Value.Value
        desc.TypeDefinition = ua.TwoByteNodeId(ua.ObjectIds.FolderType)
        return desc

    def add_item(
        self,
        desc: ua.ReferenceDescription,
        parent: QStandardItem | None = None,
        node: SyncNode | None = None,
    ) -> None:
        dname = bname = nodeid = "No Value"
        if desc.DisplayName:
            dname = desc.DisplayName.Text
        if desc.BrowseName:
            bname = desc.BrowseName.to_string()
        nodeid = desc.NodeId.to_string()
        item = [QStandardItem(dname), QStandardItem(bname), QStandardItem(nodeid)]
        if desc.NodeClass == ua.NodeClass.Object:
            if desc.TypeDefinition == ua.TwoByteNodeId(ua.ObjectIds.FolderType):
                item[0].setIcon(QIcon(":/folder.svg"))
            else:
                item[0].setIcon(QIcon(":/object.svg"))
        elif desc.NodeClass == ua.NodeClass.Variable:
            if desc.TypeDefinition == ua.TwoByteNodeId(ua.ObjectIds.PropertyType):
                item[0].setIcon(QIcon(":/property.svg"))
            else:
                item[0].setIcon(QIcon(":/variable.svg"))
        elif desc.NodeClass == ua.NodeClass.Method:
            item[0].setIcon(QIcon(":/method.svg"))
        elif desc.NodeClass == ua.NodeClass.ObjectType:
            item[0].setIcon(QIcon(":/object_type.svg"))
        elif desc.NodeClass == ua.NodeClass.VariableType:
            item[0].setIcon(QIcon(":/variable_type.svg"))
        elif desc.NodeClass == ua.NodeClass.DataType:
            item[0].setIcon(QIcon(":/data_type.svg"))
        elif desc.NodeClass == ua.NodeClass.ReferenceType:
            item[0].setIcon(QIcon(":/reference_type.svg"))
        if node:
            item[0].setData(node, Qt.ItemDataRole.UserRole)
        else:
            assert parent is not None
            parent_node = parent.data(Qt.ItemDataRole.UserRole)
            item[0].setData(new_node(parent_node, desc.NodeId), Qt.ItemDataRole.UserRole)
        # Track every node we hand out, regardless of which path created
        # it. The expand-all worker uses this to resolve the parent row
        # for a batch of children it just fetched.
        self._node_to_item[item[0].data(Qt.ItemDataRole.UserRole).nodeid.to_string()] = item[0]
        # Cache whether the node can have children at all, so the view
        # doesn't draw an expand arrow on leaf node types before they
        # are fetched. ``set_root_node`` also routes through here, so
        # the root gets the same treatment.
        item[0].setData(
            _node_class_can_have_children(desc.NodeClass),
            _CAN_HAVE_CHILDREN_ROLE,
        )
        # Cache the NodeClass int so the CSV export can pick it up
        # without a ``read_node_class`` server round-trip per row.
        # Path is *not* cached here — it's computed at export time
        # by walking the source-model parent chain (see
        # ``TreeWidget._path_for``), per the user's preference to
        # keep the model free of fields the tree doesn't render.
        item[0].setData(int(desc.NodeClass), _NODE_CLASS_ROLE)
        if parent:
            parent.appendRow(item)
        else:
            self.appendRow(item)

    def add_fetched_children(
        self,
        parent_node: SyncNode,
        descs: list[ua.ReferenceDescription],
    ) -> None:
        """Append a batch of children to ``parent_node`` and mark it fetched.

        Mirrors the bookkeeping in :meth:`fetchMore` so collapsing and
        re-expanding a row will see the node in ``_fetched`` and skip
        another ``get_children_descriptions`` call. The model-level
        ``_fetched`` list lives on the GUI thread, which is why this
        method (and the worker that drives it) must be called from there.
        """
        parent_item = self._node_to_item.get(parent_node.nodeid.to_string())
        if parent_item is None:
            raise KeyError(parent_node)
        # Idempotency guard: if a duplicate batch arrives for an
        # already-fetched parent (race between worker and view, or a
        # server that surfaces the same NodeId via multiple references)
        # we'd otherwise append the children a second time.
        if parent_node in self._fetched:
            return
        for desc in descs:
            self.add_item(desc, parent=parent_item, node=None)
        self._fetched.append(parent_node)

    def index_of_node(self, node: SyncNode) -> QModelIndex | None:
        """Return the column-0 source index of ``node``, or ``None`` if
        the model no longer knows about it (e.g. the tree was cleared
        while a worker batch was in flight).
        """
        item = self._node_to_item.get(node.nodeid.to_string())
        if item is None:
            return None
        idx = self.indexFromItem(item)
        if not idx.isValid():
            return None
        return idx.sibling(idx.row(), 0)

    def unexpanded_descendants(self, root_idx: QModelIndex) -> list[SyncNode]:
        """Return every node under ``root_idx`` whose children have not
        been fetched yet.

        Used by :meth:`TreeWidget.expand_all_async` to seed the worker
        queue for a retry. The walk goes left-to-right, depth-first;
        when it hits a node that the model already considers fetched
        (i.e. its children are installed), it recurses into those
        children. When it hits an un-fetched node, it records the
        node and stops — we cannot know the node's children without
        a server round-trip, so any descendants are unreachable until
        the worker browses it. The returned list is the worker's
        starting queue: every entry is a parent whose Browse is
        required to make further progress.

        On a freshly-loaded tree (root not yet expanded), this
        returns ``[root]``; on a tree where the previous walk
        succeeded, it returns ``[]``.
        """
        result: list[SyncNode] = []

        def walk(idx: QModelIndex) -> None:
            item = self.itemFromIndex(idx)
            if item is None:
                return
            node = item.data(Qt.ItemDataRole.UserRole)
            if node is None:
                return
            if node not in self._fetched:
                result.append(node)
                return
            for row in range(item.rowCount()):
                # ``QModelIndex.child`` is PyQt5-only; in PyQt6 the
                # canonical way to build a child index is
                # ``QAbstractItemModel.index(row, column, parent)``.
                walk(self.index(row, 0, idx))

        walk(root_idx)
        return result

    def reset_cache(self, node: SyncNode) -> None:
        if node in self._fetched:
            self._fetched.remove(node)

    def canFetchMore(self, idx: QModelIndex) -> bool:
        item = self.itemFromIndex(idx)
        if not item:
            return False
        node = item.data(Qt.ItemDataRole.UserRole)
        if node is None:
            return False
        return node not in self._fetched

    def fetchMore(self, idx: QModelIndex) -> None:
        parent = self.itemFromIndex(idx)
        if not parent:
            return
        node = parent.data(Qt.ItemDataRole.UserRole)
        if node is not None and node not in self._fetched:
            self._fetched.append(node)
        self._fetchMore(parent)

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        item = self.itemFromIndex(parent)
        if not item:
            return True
        node = item.data(Qt.ItemDataRole.UserRole)
        if node in self._fetched:
            # Already browsed: defer to the model. rowCount == 0 means
            # the server really has no children for this node.
            return QStandardItemModel.hasChildren(self, parent)
        # Not yet browsed: only optimistically show the expand arrow
        # for node classes that can actually contain children. Leaf
        # types (Variable, Method, DataType, ReferenceType) get no
        # arrow, so the user isn't baited into clicking a row that
        # would just trigger a no-op fetch.
        can = item.data(_CAN_HAVE_CHILDREN_ROLE)
        if can is False:
            return False
        return True

    def _fetchMore(self, parent: QStandardItem) -> None:
        try:
            node = parent.data(Qt.ItemDataRole.UserRole)
            descs = node.get_children_descriptions()
            descs.sort(key=lambda x: x.BrowseName)
            added: list[ua.NodeId] = []
            for desc in descs:
                if desc.NodeId not in added:
                    self.add_item(desc, parent)
                    added.append(desc.NodeId)
        except Exception as ex:
            self.error.emit(ex)
            raise

    def mimeData(self, idxs: Iterable[QModelIndex]) -> QMimeData:
        mdata = QMimeData()
        nodes: list[str] = []
        for idx in idxs:
            if not idx.isValid():
                continue
            # QSortFilterProxyModel.mimeData maps proxy indexes to source
            # indexes before calling the source model's mimeData, so the
            # indexes we receive here are always source-side.
            item = self.itemFromIndex(idx)
            if item is None:
                continue
            node = item.data(Qt.ItemDataRole.UserRole)
            if node:
                nodes.append(node.nodeid.to_string())
        mdata.setText(", ".join(nodes))
        return mdata


@dataclass(frozen=True)
class _FetchBatch:
    """One BFS layer's worth of children, emitted by :class:`_ExpandAllWorker`.

    A single ``fetch_requested`` signal carries the children fetched for
    every parent the worker browsed in the most recent batch. Each
    ``groups`` entry is a ``(parent_node, descs)`` pair the GUI uses to
    install rows under the correct parent. Bundling the whole layer
    keeps the signal traffic to one message per BFS round, regardless
    of how many parents were bundled in the underlying ``Browse`` call.
    """

    groups: list[tuple[SyncNode, list[ua.ReferenceDescription]]]
    current_path: str


class _ExpandAllWorker(QObject):
    """Background walker for the recursive Expand-All action.

    Lives on a dedicated QThread; only does asyncua I/O and emits
    signals. The GUI thread turns each ``fetch_requested`` into model
    rows and ``setExpanded`` calls.

    Iterative BFS over a deque. Each round pops up to ``_batch_size``
    parents and submits them to ``Client.browse_nodes`` in a single
    Browse service call (the "batch interface" the optimisation was
    after). The batch size starts at ``INITIAL_BATCH`` (one parent
    per Browse) and is grown adaptively: doubled on every clean
    success up to ``MAX_BATCH``, and reset to the size that actually
    worked when a Browse forces an in-iteration halving. A
    reconnect (the auto-reconnect supervisor re-establishing the
    session mid-walk) resets the size to ``INITIAL_BATCH`` as well,
    since the new session may have a different continuation-point
    pool. Children are then appended to the back of the deque; the
    loop ends when the deque is empty. NodeId-keyed dedup is shared
    with the previous DFS implementation: ``new_node`` builds a fresh
    wrapper every time, so the same logical node can appear under
    different Python ``id()``s and must be deduped by NodeId, not by
    object identity.

    Per-parent continuation points: ``Client.browse_nodes`` does not
    loop ``browse_next`` (it always sets
    ``RequestedMaxReferencesPerNode=0`` and stops at the first page).
    When the server returns a non-empty ``ContinuationPoint`` for one
    of the batched parents we follow it with a per-parent
    ``browse_next`` call. The main batched request itself stays
    un-changed; only the per-parent overflow is unbatched. This
    matches ``Node.get_children_descriptions``'s semantics (which is
    the one-at-a-time path the existing tree uses for its lazy load)
    so we don't silently drop children for any node whose reference
    count exceeds the server's per-page cap.
    """

    INITIAL_BATCH = 1
    MAX_BATCH = 16
    # When the batch has been reduced to a single parent (always the
    # case for ``normal`` mode, or after a fast-mode batch was halved
    # all the way down), retry the same Browse request up to this many
    # times before skipping the parent. Transient errors (server CPU
    # spikes, brief network blips not flagged by the
    # ``UaClientState`` disconnect check) often resolve on the next
    # attempt; persisting errors are skipped with a warning log so the
    # walk can carry on. Matches the existing per-node BadStatusCode
    # "skipping" behaviour, which also logs and continues without
    # surfacing to the GUI.
    MAX_SINGLE_RETRIES = 3
    SINGLE_RETRY_DELAY = 0.2  # seconds between single-parent retries

    fetch_requested = pyqtSignal(object)  # _FetchBatch
    progress = pyqtSignal(int, str)  # (visited_count, current_path)
    # Emitted before every ``finished`` so the GUI knows the final
    # walk totals (visited, skipped). Emitted on every terminal status
    # — not just ``ok`` — so the GUI can also report partial work
    # after a cancel or error without parsing logs.
    summary = pyqtSignal(int, int)  # (visited_count, skipped_count), terminal
    finished = pyqtSignal(str)  # "ok" | "cancelled" | "error"
    failed = pyqtSignal(object)  # Exception

    def __init__(self, client: SyncClient, mode: str = "fast") -> None:
        super().__init__()
        if mode not in ("fast", "normal"):
            raise ValueError(f"Unknown expand-all mode: {mode!r}")
        self._client = client
        # ``_sync_browse`` is the synchronous wrapper around the
        # async ``UaClient.browse`` coroutine, bound to the same
        # threadloop the rest of the app uses. ``browse_next`` is
        # symmetric; both must be invoked from the worker thread
        # via this binding so the request goes through the same
        # event loop the connection was opened on. The pattern
        # mirrors asyncua's own ``sync_uaclient_method`` helper:
        # ``sync_wrapper`` takes the unbound method and we feed
        # the uaclient in as the implicit ``self`` via
        # ``functools.partial``.
        import functools
        uaclient = client.aio_obj.uaclient
        self._sync_browse = functools.partial(
            sync_wrapper(_AsyncUaClient.browse), client.tloop, uaclient
        )
        self._sync_browse_next = functools.partial(
            sync_wrapper(_AsyncUaClient.browse_next), client.tloop, uaclient
        )
        self._cancelled: bool = False
        self._browsed: set[str] = set()  # NodeId.to_string() values
        self._queue: deque[SyncNode] = deque()
        self._visited: int = 0
        # Parents the worker dropped during the walk: either a per-node
        # StatusCode came back not-good, or the per-node retry budget
        # was exhausted. Surfaced via ``summary`` so the GUI can warn
        # the user when a walk closed cleanly but silently skipped
        # parts of the tree.
        self._skipped_count: int = 0
        # "fast" walks the tree in BFS order with an adaptive batch
        # size: starts at ``INITIAL_BATCH`` (one parent per Browse),
        # doubles on every successful outer iteration up to
        # ``MAX_BATCH``, and is reduced on Browse failures (see
        # ``run``). "normal" browses one parent at a time in DFS
        # order, replicating the semantics of the original on-demand
        # ``fetchMore`` walk that the GUI used before the BFS batch
        # worker landed; some servers (S7-1500 with shallow
        # ContinuationPoint pools) only tolerate the unbatched form.
        self._mode: str = mode
        # Current batch size for the next outer iteration in ``fast``
        # mode. Reset to ``INITIAL_BATCH`` on ``start()`` and on a
        # reconnect; grows on clean success, shrinks when a Browse
        # forces an in-iteration halving. Unused in ``normal`` mode.
        self._batch_size: int = self.INITIAL_BATCH

    @pyqtSlot()
    def cancel(self) -> None:
        # The GUI thread writes a bool; safe under CPython's GIL and
        # matches the pattern used by the asyncua state-listener
        # callback in uaclient/uaclient.py.
        self._cancelled = True

    def start(self, start_nodes: list[SyncNode]) -> None:
        """Reset state for a fresh expand-all rooted at ``start_nodes``.

        A single-node list corresponds to the "expand everything from
        here" case; multiple nodes are used for a retry that should
        skip the already-browsed ancestors (see
        :meth:`TreeViewModel.unexpanded_descendants` and
        :meth:`TreeWidget.expand_all_async`).
        """
        self._cancelled = False
        self._browsed = set()
        self._queue = deque(start_nodes)
        self._visited = 0
        self._skipped_count = 0
        # Reset adaptive batch size: we have no information about how
        # big a Browse this server will tolerate until we have tried
        # one, so the first iteration starts at the safe minimum.
        self._batch_size = self.INITIAL_BATCH

    def _pop_next(self) -> SyncNode:
        """Pop the next parent to browse.

        ``normal`` (DFS) mode pops from the right end of the deque
        (LIFO) so the walk goes depth-first and matches the original
        on-demand ``QTreeView.expandAll`` semantics. ``fast`` (BFS)
        mode pops from the left end (FIFO) for level-by-level
        expansion.
        """
        if self._mode == "normal":
            return self._queue.pop()
        return self._queue.popleft()

    def _push_children(
        self, parent: SyncNode, deduped: list[ua.ReferenceDescription]
    ) -> None:
        """Queue the children of ``parent`` for later browsing.

        In ``normal`` (DFS) mode children are pushed in reverse so
        the leftmost sibling is popped first, matching left-to-right
        depth-first order. In ``fast`` (BFS) mode children are pushed
        in source order so the queue is processed level by level.
        """
        if self._mode == "normal":
            for d in reversed(deduped):
                self._queue.append(new_node(parent, d.NodeId))
            return
        for d in deduped:
            self._queue.append(new_node(parent, d.NodeId))

    def _restore_batch(self, batch: list[SyncNode]) -> None:
        """Put ``batch`` back on ``_queue`` after a Browse failure that
        we're going to retry.

        The parents in ``batch`` were popped from ``_queue`` and added
        to ``_browsed`` before the Browse call; if we leave them in
        ``_browsed`` the next iteration would skip them as duplicates
        and we'd silently lose this layer of the tree. Clear their
        dedup marks and push them back on ``_queue`` in their original
        left-to-right order. Push back in reverse so a sequence of
        ``append``/``appendleft`` calls leaves the batch in
        first-pushed-at-the-front order. ``DFS`` mode puts them back
        on the top of the stack (the right end) so the next iteration
        pops them in the same order; ``BFS`` puts them on the left
        end so the next iteration pops them in the same order.
        """
        for node in batch:
            self._browsed.discard(node.nodeid.to_string())
        push = (
            self._queue.append if self._mode == "normal" else self._queue.appendleft
        )
        for node in reversed(batch):
            push(node)

    def _is_disconnected(self) -> bool:
        """Return True if the asyncua client can't service a Browse
        right now.

        Read the live ``UaClientState`` rather than catching a
        specific exception class: any error coming back from a
        Browse on a disconnecting/reconnecting socket surfaces as
        some flavour of ``ConnectionError`` / ``OSError`` / socket
        error, and the exact type varies across asyncua versions and
        transports. The state machine is the canonical signal and
        is robust to that churn.
        """
        try:
            state = self._client.aio_obj.uaclient.state
        except Exception:
            # If we can't even read the state, treat it as gone.
            return True
        return state in (
            UaClientState.DISCONNECTED,
            UaClientState.RECONNECTING,
            UaClientState.CONNECTING,
            UaClientState.DISCONNECTING,
        )

    def _await_reconnect(self) -> bool:
        """Block the worker thread until the client returns to
        ``CONNECTED``, the user cancels, or the user disconnects.

        The asyncua state listener fires on the asyncua thread; the
        worker can't easily subscribe to it from here without
        cross-thread plumbing. Polling at 100 ms is good enough:
        reconnect latencies are on the order of seconds, and the
        worker has nothing else to do while it waits.

        Returns True on reconnect, False on cancel (user-driven
        ``disconnect`` flips ``_cancelled`` via ``cancel_expand``).
        """
        while not self._cancelled:
            try:
                state = self._client.aio_obj.uaclient.state
            except Exception:
                # State went away under us; the next Browse would
                # just fail again. Bail out so the worker can emit
                # an error rather than spin forever.
                return False
            if state == UaClientState.CONNECTED:
                return True
            time.sleep(0.1)
        return False

    def _browse_next_for(self, continuation_point: bytes) -> ua.BrowseResult | None:
        """Follow a single BrowseNext continuation point to completion.

        Mirrors ``Node._browse_next`` from asyncua but used here from
        the worker thread, one parent at a time. Returns ``None`` if
        the server signals cancellation; otherwise the final
        ``BrowseResult`` with an empty ``ContinuationPoint``.
        """
        params = BrowseNextParameters()
        params.ContinuationPoints = [continuation_point]
        params.ReleaseContinuationPoints = False
        try:
            results = self._sync_browse_next(params)
        except Exception as ex:
            logger.warning("browse_next failed: %s", ex)
            return None
        if not results:
            return None
        return results[0]

    def _browse_hierarchical(
        self, parents: list[SyncNode]
    ) -> list[tuple[SyncNode, ua.BrowseResult]]:
        """One Browse call restricted to forward HierarchicalReferences.

        ``Client.browse_nodes`` is unsuitable for the expand-all
        walk: it sets ``ReferenceTypeId=Null`` on every
        ``BrowseDescription`` and the server then returns edges of
        every reference type — including HasTypeDefinition, which
        points at the type definition of each node and shows up in
        the tree as an extra "BaseObjectType" / "BaseDataVariableType"
        child row. This helper reproduces the per-node behaviour of
        ``Node.get_children_descriptions`` (which sets
        ``ReferenceTypeId=HierarchicalReferences`` and
        ``BrowseDirection=Forward``) but in a single batched call.

        ``params.View.Timestamp`` is set to the current UTC time to
        mirror ``Node.get_references`` exactly — without it some
        servers (notably the MicroStep server on ``msnc500w3510:62548``
        which the user reports succeeds on manual expand but fails on
        Expand All) reject the request with BadViewIdUnknown despite
        the ViewId being the same default ``NumericNodeId(0)``.
        ``ViewId`` itself is left at its default; explicit
        ``ViewId=None`` was tried and regressed freeopcua's encoder.
        """
        params = BrowseParameters()
        params.View.Timestamp = ua.get_win_epoch()
        params.RequestedMaxReferencesPerNode = 0
        params.NodesToBrowse = []
        for node in parents:
            desc = BrowseDescription()
            desc.NodeId = node.nodeid
            desc.BrowseDirection = BrowseDirection.Forward
            desc.ReferenceTypeId = ua.TwoByteNodeId(ObjectIds.HierarchicalReferences)
            desc.IncludeSubtypes = True
            desc.NodeClassMask = NodeClass.Unspecified
            desc.ResultMask = BrowseResultMask.All
            params.NodesToBrowse.append(desc)
        results = self._sync_browse(params)
        return list(zip(parents, results))

    @pyqtSlot()
    def run(self) -> None:
        try:
            while self._queue:
                if self._cancelled:
                    self.summary.emit(self._visited, self._skipped_count)
                    self.finished.emit("cancelled")
                    return

                # Pop up to the current batch size from ``_queue``, skipping any
                # NodeId we've already walked. In ``fast`` (BFS) mode the
                # batch grows adaptively from 1 up to ``MAX_BATCH`` based
                # on how the server tolerates Browse requests; in
                # ``normal`` (DFS) mode we always go one parent at a
                # time, matching the original unbatched semantics.
                batch: list[SyncNode] = []
                target_batch = 1 if self._mode == "normal" else self._batch_size
                while self._queue and len(batch) < target_batch:
                    node = self._pop_next()
                    node_key = node.nodeid.to_string()
                    if node_key in self._browsed:
                        continue
                    self._browsed.add(node_key)
                    batch.append(node)
                # Credit ``_visited`` at pop time rather than after a
                # successful Browse, so the progress dialog shows a
                # monotonically growing count whose per-iteration
                # increment is the current ``_batch_size`` (predictable
                # for the user) rather than the post-halving remainder
                # (which can vary 1…16 round-to-round when the inner
                # retry path halves the batch). Deferred halves get
                # re-popped and re-counted on a later iteration; the
                # total Browse work done is unchanged, the displayed
                # number is just a touch higher than the unique-node
                # count. This matches what the user asked for: a
                # running total that goes up steadily and never resets
                # mid-walk.
                self._visited += len(batch)
                if not batch:
                    continue
                # Snapshot the size we actually tried; the inner retry
                # loop may shrink ``batch`` by half (and push the rest
                # back to the queue), and we need to know whether the
                # final ``Browse`` succeeded at the original size or
                # only after halving in order to update ``_batch_size``
                # appropriately below.
                initial_batch_size = len(batch)

                # One Browse service call covering every parent in the
                # batch. We can't use ``Client.browse_nodes`` here
                # because that helper sets ``ReferenceTypeId=Null``
                # (i.e. all reference types), which causes the server
                # to return HasTypeDefinition edges as if they were
                # children — every Object/Variable would gain an extra
                # "BaseObjectType" / "BaseDataVariableType" row under
                # it. ``Node.get_children_descriptions`` solves the
                # same problem by passing
                # ``ReferenceTypeId=HierarchicalReferences`` and
                # ``Direction=Forward``; we mirror that exactly so the
                # BFS view matches what the lazy-load view shows.
                #
                # Failure handling: the cause matters.
                #
                # * Disconnect mid-walk: the auto-reconnect
                #   supervisor will re-establish the session in the
                #   background. We don't want to give up just because
                #   a network blip landed in the middle of the
                #   expansion. Push the batch back to the front of
                #   ``_queue`` (and clear its ``_browsed`` marks) so
                #   the next iteration re-tries the same Browse once
                #   the link is back up, and poll
                #   ``UaClientState`` until it returns to CONNECTED
                #   or the user cancels.
                # * Any other Browse error: try halving the batch and
                #   retrying. Some servers (notably the Siemens
                #   S7-1500 line) reject the entire batch with
                #   BadNoContinuationPoints when the request would
                #   otherwise exhaust the server's continuation-point
                #   pool; the per-node StatusCode from
                #   ``_browse_hierarchical`` would have shown
                #   "skipping" for the offending node, but on those
                #   servers we never get that far because the batch
                #   itself is rejected with "Unhandled exception"
                #   before any per-node results come back. Halving
                #   lets the walk proceed at a batch size the server
                #   actually accepts; the deferred half is pushed
                #   back to the front of the queue so it gets retried
                #   in its own outer-loop iteration. If halving all
                #   the way down to a single parent still fails,
                #   retry the same parent a few times to ride out
                #   transient errors, then skip it and let the walk
                #   carry on. The skip behaviour matches the existing
                #   per-node BadStatusCode "skipping" branch, which
                #   also logs and continues without surfacing to the
                #   GUI.
                reconnect_retry = False
                single_retry = 0
                while True:
                    try:
                        results = self._browse_hierarchical(batch)
                        break
                    except Exception as ex:
                        # Log the NodeIds of every parent in the failed
                        # batch so the operator can correlate this with
                        # a specific subtree. Including the parent list
                        # is the only signal that lets us narrow the
                        # failure down to a specific subtree.
                        parents_repr = ", ".join(
                            n.nodeid.to_string() for n in batch
                        )
                        if self._is_disconnected():
                            self._restore_batch(batch)
                            # Reset adaptive batch size: after a
                            # reconnect the new session may have a
                            # different (usually smaller) continuation-
                            # point pool than the old one, so we want
                            # to rediscover the right size from
                            # scratch rather than resume at whatever
                            # value the old session tolerated.
                            self._batch_size = self.INITIAL_BATCH
                            logger.warning(
                                "Expand-all batch browse failed (%s) on batch "
                                "[%s]; pausing until the session is reconnected",
                                ex, parents_repr,
                            )
                            # Update the progress dialog so the user can
                            # tell the walk is still alive but waiting.
                            self.progress.emit(
                                self._visited,
                                "(paused — waiting for reconnect)",
                            )
                            if self._await_reconnect() and not self._cancelled:
                                # Session back up; the next outer-loop
                                # iteration re-pops the same batch and
                                # retries the Browse.
                                reconnect_retry = True
                                break
                            # Cancelled (or reconnect gave up).
                            self.summary.emit(self._visited, self._skipped_count)
                            self.finished.emit("cancelled")
                            return
                        if len(batch) <= 1:
                            # Can't halve further. In ``normal`` mode
                            # this branch is the only failure path;
                            # in ``fast`` mode it's the terminal case
                            # after the batch was halved down to a
                            # single parent. Retry the same parent a
                            # few times to ride out transient errors;
                            # if still failing, skip it and let the
                            # outer loop carry on with the next node.
                            if single_retry < self.MAX_SINGLE_RETRIES:
                                single_retry += 1
                                logger.info(
                                    "Browse for %s failed (%s); "
                                    "retrying (%d/%d)",
                                    parents_repr, ex,
                                    single_retry, self.MAX_SINGLE_RETRIES,
                                )
                                self.progress.emit(
                                    self._visited,
                                    f"(retrying {single_retry}/"
                                    f"{self.MAX_SINGLE_RETRIES}: "
                                    f"{parents_repr})",
                                )
                                time.sleep(self.SINGLE_RETRY_DELAY)
                                if self._cancelled:
                                    self.summary.emit(self._visited, self._skipped_count)
                                    self.finished.emit("cancelled")
                                    return
                                # Loop back and retry the same parent.
                                continue
                            logger.warning(
                                "Expand-all browse failed (%s) on %s after "
                                "%d retries; skipping and continuing the walk",
                                ex, parents_repr, self.MAX_SINGLE_RETRIES,
                            )
                            self._skipped_count += 1
                            # Empty results so the result-processing
                            # block below installs no children for
                            # this parent but still increments
                            # ``_visited`` and emits a progress tick.
                            results = []
                            break
                        mid = len(batch) // 2
                        second_half = batch[mid:]
                        batch = batch[:mid]
                        # Clear ``_browsed`` marks on the deferred
                        # half so they get re-popped in their own
                        # outer-loop iteration; without this, the
                        # dedup guard in the outer loop would silently
                        # drop them on the next pop.
                        for n in second_half:
                            self._browsed.discard(n.nodeid.to_string())
                        # BFS pops from the left, so push the deferred
                        # half back to the left in reverse to preserve
                        # the original pop order. (DFS mode uses
                        # ``batch_size=1``, so this branch never runs
                        # there.)
                        for n in reversed(second_half):
                            self._queue.appendleft(n)
                        if self._cancelled:
                            self.summary.emit(self._visited, self._skipped_count)
                            self.finished.emit("cancelled")
                            return
                        logger.info(
                            "Browse failed with %d parents; halving batch "
                            "and retrying with %d (deferred %d to next round)",
                            len(batch) + len(second_half),
                            len(batch),
                            len(second_half),
                        )
                        self.progress.emit(
                            self._visited,
                            f"(halving adaptive batch: retrying with {len(batch)} parents)",
                        )
                        # Loop back and retry with the now-smaller batch.
                if reconnect_retry:
                    continue

                # Adaptive batch sizing for the next outer iteration.
                # A clean success (no halving) doubles ``_batch_size``
                # up to ``MAX_BATCH``; a success that only landed
                # after halving keeps ``_batch_size`` at the size that
                # actually worked, so we don't immediately retry the
                # size the server just rejected. The reconnect path
                # already resets ``_batch_size`` to ``INITIAL_BATCH``
                # before the await, so it doesn't reach here.
                if len(batch) < initial_batch_size:
                    self._batch_size = len(batch)
                else:
                    self._batch_size = min(
                        self._batch_size * 2, self.MAX_BATCH
                    )

                groups: list[tuple[SyncNode, list[ua.ReferenceDescription]]] = []
                for parent, br in results:
                    if not br.StatusCode.is_good():
                        # Surface the symbolic name + spec doc string alongside
                        # the raw StatusCode value: BadNoContinuationPoints
                        # (the typical S7-1500 symptom) reads as a generic
                        # 0x804B0000 otherwise and gives no hint that the
                        # server has run out of continuation points. Without
                        # this the user sees "skipping" with no actionable
                        # detail; with it they immediately recognise a
                        # server-side resource issue and can release CPs by
                        # reconnecting / giving the server time to GC.
                        sc = br.StatusCode
                        logger.warning(
                            "Browse for %s returned %s (%s: %s); skipping",
                            parent.nodeid, sc, sc.name, sc.doc,
                        )
                        self._skipped_count += 1
                        continue
                    descs = list(br.References)
                    cont = br.ContinuationPoint
                    while cont:
                        if self._cancelled:
                            self.summary.emit(self._visited, self._skipped_count)
                            self.finished.emit("cancelled")
                            return
                        next_br = self._browse_next_for(cont)
                        if next_br is None:
                            break
                        descs.extend(next_br.References)
                        cont = next_br.ContinuationPoint
                    # Match _fetchMore's BrowseName ordering and
                    # de-duplicate by NodeId so duplicate child rows
                    # don't end up in the tree (some servers return
                    # the same reference twice). Sort on the qualified
                    # name components rather than the QualifiedName
                    # itself: a few server implementations return
                    # BrowseName=None, and QualifiedName's __lt__ on
                    # a None ``Name`` raises TypeError. In BFS a
                    # single bad reference in a batch would otherwise
                    # abort the whole layer.
                    descs.sort(key=_browse_name_sort_key)
                    seen: set[ua.NodeId] = set()
                    deduped: list[ua.ReferenceDescription] = []
                    for d in descs:
                        if d.NodeId in seen:
                            continue
                        seen.add(d.NodeId)
                        deduped.append(d)
                    groups.append((parent, deduped))
                    self._push_children(parent, deduped)

                if self._cancelled:
                    self.summary.emit(self._visited, self._skipped_count)
                    self.finished.emit("cancelled")
                    return
                # The current_path is purely cosmetic; reporting the
                # first parent in the batch is enough to show the
                # user roughly where the walk is.
                current_path = _format_path(batch[0])
                self.fetch_requested.emit(
                    _FetchBatch(groups=groups, current_path=current_path)
                )
                # ``_visited`` was credited at pop time so the dialog
                # count grows monotonically with the current
                # ``_batch_size`` (see the pop block above); only
                # re-emit progress here so the latest ``current_path``
                # gets displayed.
                self.progress.emit(self._visited, current_path)
            self.summary.emit(self._visited, self._skipped_count)
            self.finished.emit("ok")
        except Exception as ex:
            # Defensive: anything that escapes the inner try above
            # still needs to drive the GUI off the wait.
            logger.exception("Expand-all worker crashed")
            self.failed.emit(ex)
            self.summary.emit(self._visited, self._skipped_count)
            self.finished.emit("error")


def _browse_name_sort_key(desc: ua.ReferenceDescription) -> tuple[int, str, str]:
    """Sort key for ``ReferenceDescription`` by BrowseName that tolerates None.

    Some OPC-UA servers return a ``BrowseName`` whose ``Name`` field is
    ``None`` (e.g. when the type definition is missing). Sorting on
    the bare ``QualifiedName`` would raise ``TypeError`` from
    ``QualifiedName.__lt__``; sorting on a tuple of namespace index +
    name string falls back to a stable secondary order and never
    raises. Empty string sorts before non-empty, so a None browse
    name lands at the top of the parent.
    """
    bn = desc.BrowseName
    if bn is None:
        return (-1, "", desc.NodeId.to_string())
    return (bn.NamespaceIndex, bn.Name or "", desc.NodeId.to_string())


def _format_path(node: SyncNode) -> str:
    """Best-effort '/'-joined BrowseName path for status display."""
    try:
        return "/".join(p.read_browse_name().Name for p in node.get_path())
    except Exception:
        try:
            return node.nodeid.to_string()
        except Exception:
            return "?"