import logging
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse, urlunparse

from PyQt6.QtCore import QObject, QSettings, QStandardPaths, pyqtSignal

from asyncua import crypto, ua
from asyncua.client.ua_client import UaClientState
from asyncua.sync import Client, SyncNode, ThreadLoop
from asyncua.tools import endpoint_to_strings


logger = logging.getLogger(__name__)


def _force_ipv4_hostname(uri: str) -> str:
    """Resolve the URI hostname to an IPv4 literal so Windows Proactor
    doesn't burn its connect timeout trying ::1 first.

    IP literals (v4 / v6) and unresolvable hostnames are returned
    unchanged so we don't lock out IPv6-only setups.
    """
    parsed = urlparse(uri)
    host = parsed.hostname
    if host is None:
        return uri
    # IP literals bypass DNS resolution; leave them alone.
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return uri
        except OSError:
            continue
    try:
        infos = socket.getaddrinfo(host, parsed.port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return uri
    if not infos:
        return uri
    ipv4 = infos[0][4][0]
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username + (f":{parsed.password}" if parsed.password else "") + "@"
    netloc = f"{userinfo}{ipv4}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))

AuthMode = Literal["anonymous", "username", "certificate"]


class UaClient(QObject):
    """
    OPC-Ua client specialized for the need of GUI client
    return exactly what GUI needs, no customization possible
    """

    # Forwarded from asyncua's connection state. Value is a UaClientState string
    # (e.g. "connected", "reconnecting", "disconnected"). Emitted from the
    # asyncua thread, so connect with QueuedConnection.
    connection_state_changed = pyqtSignal(str)

    def __init__(self) -> None:
        QObject.__init__(self)
        self.settings = QSettings()
        self.application_uri = "urn:freeopcua:client-gui"
        # One ThreadLoop is shared across every Client we open. sync.Client
        # owns and stops its threadloop on disconnect() when it created it,
        # which would tear down the asyncio loop our session lives on; passing
        # our own tloop keeps it alive across connect/disconnect cycles.
        self._tloop = ThreadLoop()
        self._tloop.start()
        self.client: Client | None = None
        self._connected = False
        self._datachange_sub: Any = None
        self._event_sub: Any = None
        self._subs_dc: dict[ua.NodeId, Any] = {}
        self._subs_ev: dict[ua.NodeId, Any] = {}
        self._unsubscribe_state: Callable[[], None] | None = None
        self.security_mode: str | None = None
        self.security_policy: str | None = None
        self.user_certificate_path: str | None = None
        self.user_private_key_path: str | None = None
        self.application_certificate_path: str | None = None
        self.application_private_key_path: str | None = None
        self.auth_mode: AuthMode = "anonymous"
        self.username: str | None = None
        self.password: str | None = None
        self.endpoint_url: str | None = None
        self.load_application_certificate_settings()
        # Background worker for the best-effort custom-type load kicked off
        # at the end of connect(). Tracked so a second connect() doesn't
        # spawn a duplicate worker.
        self._custom_types_thread: threading.Thread | None = None

    def shutdown(self) -> None:
        """Tear down the shared ThreadLoop. Call once on application exit."""
        try:
            self._tloop.stop()
        except Exception:
            logger.exception("Failed to stop ThreadLoop")

    def _reset(self) -> None:
        if self._unsubscribe_state is not None:
            try:
                self._unsubscribe_state()
            except Exception:
                logger.exception("Failed to unsubscribe from state listener")
            self._unsubscribe_state = None
        self.client = None
        self._connected = False
        self._datachange_sub = None
        self._event_sub = None
        self._subs_dc = {}
        self._subs_ev = {}

    def get_endpoints(self, uri: str) -> list[ua.EndpointDescription]:
        client = Client(_force_ipv4_hostname(uri), timeout=2, tloop=self._tloop)
        edps = client.connect_and_get_server_endpoints()
        for i, ep in enumerate(edps, start=1):
            logger.info('Endpoint %s:', i)
            for (n, v) in endpoint_to_strings(ep):
                logger.info('  %s: %s', n, v)
            logger.info('')
        return edps

    def load_security_settings(self, uri: str) -> None:
        self.security_mode = None
        self.security_policy = None
        self.user_certificate_path = None
        self.user_private_key_path = None
        self.auth_mode = "anonymous"
        self.username = None
        self.endpoint_url = None

        mysettings = self.settings.value("security_settings", None)
        if mysettings is None or uri not in mysettings:
            return
        entry = mysettings[uri]
        if isinstance(entry, list):
            mode, policy, cert, key = entry
            self.security_mode = mode
            self.security_policy = policy
            self.user_certificate_path = cert
            self.user_private_key_path = key
            return
        self.security_mode = entry.get("mode")
        self.security_policy = entry.get("policy")
        self.user_certificate_path = entry.get("user_certificate")
        self.user_private_key_path = entry.get("user_private_key")
        self.auth_mode = entry.get("auth_mode", "anonymous")
        self.username = entry.get("username")
        self.endpoint_url = entry.get("endpoint_url")

    def save_security_settings(self, uri: str) -> None:
        mysettings = self.settings.value("security_settings", None)
        if mysettings is None:
            mysettings = {}
        mysettings[uri] = {
            "mode": self.security_mode,
            "policy": self.security_policy,
            "user_certificate": self.user_certificate_path,
            "user_private_key": self.user_private_key_path,
            "auth_mode": self.auth_mode,
            "username": self.username,
            "endpoint_url": self.endpoint_url,
        }
        self.settings.setValue("security_settings", mysettings)

    def load_application_certificate_settings(self) -> None:
        self.application_certificate_path = None
        self.application_private_key_path = None

        mysettings = self.settings.value("application_certificate_settings", None)
        if mysettings is None:
            return
        self.application_certificate_path = mysettings["application_certificate"]
        self.application_private_key_path = mysettings["application_private_key"]

    def generate_application_certificate(self) -> tuple[str, str]:
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        pki_dir = (Path(base) if base else Path.home() / ".freeopcua") / "pki"
        key_file = pki_dir / "own_private_key.pem"
        cert_file = pki_dir / "own_cert.der"
        client = Client("opc.tcp://localhost:4840", tloop=self._tloop)
        client.application_uri = self.application_uri
        cert, key = client.setup_self_signed_certificate(key_file=key_file, cert_file=cert_file)
        return str(cert), str(key)

    def save_application_certificate_settings(self) -> None:
        mysettings = self.settings.value("application_certificate_settings", None)
        if mysettings is None:
            mysettings = {}
        mysettings["application_certificate"] = self.application_certificate_path
        mysettings["application_private_key"] = self.application_private_key_path
        self.settings.setValue("application_certificate_settings", mysettings)

    def get_node(self, nodeid: ua.NodeId | str) -> SyncNode:
        assert self.client is not None
        return self.client.get_node(nodeid)

    def connect(self, uri: str) -> None:
        self.disconnect()
        logger.info("Connecting to %s with parameters %s, %s, %s, %s, %s", uri, self.auth_mode, self.security_mode, self.security_policy, self.user_certificate_path, self.user_private_key_path)
        self.client = Client(_force_ipv4_hostname(uri), tloop=self._tloop)
        self.client.application_uri = self.application_uri
        self.client.description = "FreeOpcUa Client GUI"

        if self.auth_mode == "username":
            if self.username:
                self.client.set_user(self.username)
            if self.password:
                self.client.set_password(self.password)
        elif self.auth_mode == "certificate":
            # asyncua picks the certificate identity token only when no username is set
            # and a user certificate is loaded, so these stay scoped to this mode.
            if self.user_private_key_path:
                self.client.load_private_key(self.user_private_key_path)
            if self.user_certificate_path:
                self.client.load_client_certificate(self.user_certificate_path)

        if self.security_mode is not None and self.security_policy is not None:
            if not (self.application_certificate_path and self.application_private_key_path):
                self.application_certificate_path, self.application_private_key_path = (
                    self.generate_application_certificate()
                )
                self.save_application_certificate_settings()
            # Endpoint policy URIs spell Aes policies with underscores
            # (Aes256_Sha256_RsaPss); the asyncua class name omits them.
            policy_class = 'SecurityPolicy' + self.security_policy.replace('_', '')
            self.client.set_security(
                getattr(crypto.security_policies, policy_class),
                self.application_certificate_path,
                self.application_private_key_path,
                mode=getattr(ua.MessageSecurityMode, self.security_mode)
            )
        self.client.connect(auto_reconnect=True)
        self._connected = True
        self._install_state_listener()
        self.save_security_settings(uri)
        # Defer the custom-type load to a worker so a slow or unresponsive
        # server can't block the connect path or break the session: the
        # load walks the type tree with browse requests and a single
        # hang (common on older firmware that pre-dates the 1.04
        # DataTypeDefinition attribute) used to surface as a TimeoutError
        # that left the sync client in a state where the next
        # read_attributes / browse call raised "Connection is closed".
        self._schedule_load_custom_types()

    def _schedule_load_custom_types(self) -> None:
        """Defer the custom-type load to a background worker.

        The load walks the type tree with browse requests and a single
        hang (common on older firmware that pre-dates the 1.04
        DataTypeDefinition attribute) used to block the connect path
        and break the session: the TimeoutError left the sync client
        in a state where the next read_attributes / browse call raised
        "Connection is closed" before the auto-reconnect supervisor
        could finish its handshake. Running the load in a daemon
        thread keeps the connect path unblocked. The worker is
        best-effort, failures are logged, and a stuck load cannot
        block app exit. If the user disconnects while the load is in
        progress, the pending requests fail with ConnectionError and
        the worker exits cleanly via the try/except in
        ``_load_custom_types_worker``.
        """
        if self.client is None:
            return
        thread = self._custom_types_thread
        if thread is not None and thread.is_alive():
            # a previous connect's load is still in progress; let it
            # finish rather than double up.
            return
        self._custom_types_thread = threading.Thread(
            target=self._load_custom_types_worker,
            name="load-custom-types",
            daemon=True,
        )
        self._custom_types_thread.start()

    def _load_custom_types_worker(self) -> None:
        # Capture the client reference; self.client may be reset on
        # disconnect before this worker gets a chance to run.
        client = self.client
        if client is None:
            return
        try:
            client.load_data_type_definitions()
            client.load_enums()
            client.load_type_definitions()
            logger.info("Custom types loaded")
        except Exception:
            logger.exception("Loading custom types failed (server may pre-date spec 1.04)")

    def _install_state_listener(self) -> None:
        """Forward asyncua state transitions to the Qt signal.

        The listener is invoked synchronously on the asyncua thread; we only
        emit a Qt signal so the slot runs on the GUI thread (the connection
        site uses QueuedConnection).
        """
        assert self.client is not None
        uaclient = self.client.aio_obj.uaclient
        self._unsubscribe_state = uaclient._add_state_listener(self._on_state_change)

    def _on_state_change(self, state: UaClientState) -> None:
        self.connection_state_changed.emit(state.value)

    def disconnect(self) -> None:
        if self._connected:
            logger.info("Disconnecting from server")
            self._connected = False
            # Unhook the state listener first: the asyncua disconnect()
            # will walk through DISCONNECTING/DISCONNECTED, but those are
            # part of the user-initiated teardown — we don't want the
            # GUI to flash an "auto-reconnect in progress" banner.
            if self._unsubscribe_state is not None:
                try:
                    self._unsubscribe_state()
                except Exception:
                    logger.exception("Failed to unsubscribe from state listener")
                self._unsubscribe_state = None
            try:
                assert self.client is not None
                self.client.disconnect()
            finally:
                self._reset()

    def subscribe_datachange(self, node: SyncNode, handler: Any) -> Any:
        assert self.client is not None
        if not self._datachange_sub:
            self._datachange_sub = self.client.create_subscription(500, handler)
        handle = self._datachange_sub.subscribe_data_change(node)
        self._subs_dc[node.nodeid] = handle
        return handle

    def unsubscribe_datachange(self, node: SyncNode) -> None:
        self._datachange_sub.unsubscribe(self._subs_dc[node.nodeid])

    def subscribe_events(self, node: SyncNode, handler: Any) -> Any:
        assert self.client is not None
        if not self._event_sub:
            self._event_sub = self.client.create_subscription(500, handler)
        handle = self._event_sub.subscribe_events(node)
        self._subs_ev[node.nodeid] = handle
        return handle

    def unsubscribe_events(self, node: SyncNode) -> None:
        self._event_sub.unsubscribe(self._subs_ev[node.nodeid])

    def get_node_attrs(self, node: SyncNode | ua.NodeId | str) -> tuple[SyncNode, list[str]]:
        if not isinstance(node, SyncNode):
            assert self.client is not None
            node = self.client.get_node(node)
        attrs = node.read_attributes([ua.AttributeIds.DisplayName, ua.AttributeIds.BrowseName, ua.AttributeIds.NodeId])
        return node, [attr.Value.Value.to_string() for attr in attrs]

    @staticmethod
    def get_children(node: SyncNode) -> list[ua.ReferenceDescription]:
        descs = node.get_children_descriptions()
        descs.sort(key=lambda x: x.BrowseName)
        return descs
