import logging
from dataclasses import dataclass
from typing import Iterable

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
)
from PyQt6.QtGui import QStandardItemModel, QStandardItem, QIcon, QAction
from PyQt6.QtWidgets import QApplication, QAbstractItemView, QHeaderView, QLineEdit, QTreeView

from asyncua import ua
from asyncua.sync import SyncNode, new_node


logger = logging.getLogger(__name__)

# Bumped to v3: invalidate pre-proxy-model header state that may have left
# columns at zero width / hidden after wrapping the tree in a proxy.
_HEADER_STATE_KEY = "tree_widget_state_v3"

# Custom role on the column-0 QStandardItem. Set to ``True``/``False``
# from ``desc.NodeClass`` at insert time so ``hasChildren`` can avoid
# drawing a (misleading) expand arrow on leaf node types like
# ``Variable`` or ``Method`` without doing a server round-trip per row.
_CAN_HAVE_CHILDREN_ROLE = Qt.ItemDataRole.UserRole + 1


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

    def save_state(self) -> None:
        header = self.view.header()
        if header is not None:
            self.settings.setValue(_HEADER_STATE_KEY, header.saveState())

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

    def expand_current_node(self, expand: bool = True) -> None:
        idx = self.view.currentIndex()
        self.view.setExpanded(idx, expand)

    def expand_all_async(self, idx: QModelIndex | None = None) -> None:
        """Recursively expand the row at ``idx`` (or the current row) and
        all of its descendants, off the GUI thread.

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
        # Spin up a fresh worker + thread per call; auto-cleanup on
        # completion. ~10 ms spin-up cost is dwarfed by server RTT.
        self._worker = _ExpandAllWorker()
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.fetch_requested.connect(self._on_fetch_requested, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        self._worker.progress.connect(self.expand_progress, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        self._worker.finished.connect(self._on_worker_finished, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        self._worker.failed.connect(self._on_worker_failed, type=Qt.ConnectionType.QueuedConnection)  # type: ignore[call-arg]
        # Tear-down chain: finished → stop thread; thread done → free.
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._worker.start(node)
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
        thread.quit()
        # Bounded wait so a wedged worker can't hang app shutdown.
        thread.wait(2000)

    @pyqtSlot(object)
    def _on_fetch_requested(self, payload: object) -> None:
        """GUI-side handler: install a batch of children and expand them."""
        if not isinstance(payload, _FetchBatch):
            return
        if self._worker is not None and self._worker._cancelled:
            # Cancel landed mid-flight; drop the batch and let the worker's
            # ``finished("cancelled")`` tear things down.
            return
        try:
            self.model.add_fetched_children(payload.parent_node, payload.descs)
        except KeyError:
            # Disconnect / clear raced with the worker. Nothing to install;
            # the worker will wind down on its own.
            return
        # Mirror the recursive walk: expand the parent now that the
        # children are in the model, so the user sees the tree fill in.
        source_idx = self.model.index_of_node(payload.parent_node)
        if source_idx is None:
            return
        proxy_idx = self._source_to_proxy(source_idx)
        if proxy_idx.isValid():
            self.view.setExpanded(proxy_idx, True)

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
    """One node's worth of children, emitted by :class:`_ExpandAllWorker`.

    A single ``fetch_requested`` signal carries both the parent node (so
    the GUI can find the row in the model) and its freshly-fetched
    descriptions. Sending both in one payload keeps the signal traffic
    to one message per parent visited, regardless of child count.
    """

    parent_node: SyncNode
    descs: list[ua.ReferenceDescription]
    current_path: str


class _ExpandAllWorker(QObject):
    """Background walker for the recursive Expand-All action.

    Lives on a dedicated QThread; only does asyncua I/O and emits
    signals. The GUI thread turns each ``fetch_requested`` into model
    rows and ``setExpanded`` calls.

    Iterative DFS over a stack so deep trees don't blow Python's stack.
    A ``_browsed`` set keyed on the NodeId (not Python ``id()``) skips
    nodes the worker has already walked. Keying on Python id is wrong:
    ``new_node`` builds a fresh wrapper every time, so the same logical
    node would land on the stack under different object identities when
    an OPC UA server exposes the same NodeId via multiple back-reference
    paths. Without NodeId-based dedup the worker would re-browse the
    node and the GUI would append a second set of child rows.
    """

    fetch_requested = pyqtSignal(object)  # _FetchBatch
    progress = pyqtSignal(int, str)  # (visited_count, current_path)
    finished = pyqtSignal(str)  # "ok" | "cancelled" | "error"
    failed = pyqtSignal(object)  # Exception

    def __init__(self) -> None:
        super().__init__()
        self._cancelled: bool = False
        self._browsed: set[str] = set()  # NodeId.to_string() values
        self._stack: list[SyncNode] = []
        self._visited: int = 0

    @pyqtSlot()
    def cancel(self) -> None:
        # The GUI thread writes a bool; safe under CPython's GIL and
        # matches the pattern used by the asyncua state-listener
        # callback in uaclient/uaclient.py.
        self._cancelled = True

    def start(self, root_node: SyncNode) -> None:
        """Reset state for a fresh expand-all rooted at ``root_node``."""
        self._cancelled = False
        self._browsed = set()
        self._stack = [root_node]
        self._visited = 0

    @pyqtSlot()
    def run(self) -> None:
        try:
            while self._stack:
                if self._cancelled:
                    self.finished.emit("cancelled")
                    return
                node = self._stack.pop()
                # Key by NodeId, not id(node): see class docstring.
                node_key = node.nodeid.to_string()
                if node_key in self._browsed:
                    continue
                self._browsed.add(node_key)

                try:
                    descs = node.get_children_descriptions()
                except Exception as ex:
                    logger.warning("Expand-all failed at %s: %s", node, ex)
                    self.failed.emit(ex)
                    self.finished.emit("error")
                    return
                # Match _fetchMore's BrowseName ordering and de-duplicate
                # by NodeId so duplicate child rows don't end up in the
                # tree (some servers return the same reference twice).
                descs.sort(key=lambda x: x.BrowseName)
                seen: set[ua.NodeId] = set()
                deduped: list[ua.ReferenceDescription] = []
                for d in descs:
                    if d.NodeId in seen:
                        continue
                    seen.add(d.NodeId)
                    deduped.append(d)
                descs = deduped

                current_path = _format_path(node)

                self.fetch_requested.emit(
                    _FetchBatch(parent_node=node, descs=descs, current_path=current_path)
                )
                self.progress.emit(self._visited, current_path)
                self._visited += 1

                for desc in descs:
                    self._stack.append(new_node(node, desc.NodeId))
            self.finished.emit("ok")
        except Exception as ex:
            # Defensive: anything that escapes the inner try above
            # still needs to drive the GUI off the wait.
            logger.exception("Expand-all worker crashed")
            self.failed.emit(ex)
            self.finished.emit("error")


def _format_path(node: SyncNode) -> str:
    """Best-effort '/'-joined BrowseName path for status display."""
    try:
        return "/".join(p.read_browse_name().Name for p in node.get_path())
    except Exception:
        try:
            return node.nodeid.to_string()
        except Exception:
            return "?"