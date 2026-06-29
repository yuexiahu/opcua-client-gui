"""Regression test for the deferred custom-type loader.

Before the fix, ``UaClient.connect`` called
``load_data_type_definitions`` / ``load_enums`` / ``load_type_definitions``
inline. On older firmware the recursive browse walk could hang for tens
of seconds and ultimately raise a ``TimeoutError`` that left the sync
client in a state where the next ``read_attributes`` / ``browse`` call
came back with ``ConnectionError: Connection is closed`` — see the
S7-1500 reconnect log in ``opcua-client.log``.

The fix defers the load to a daemon worker thread so the connect path
returns as soon as the session is up. This test pins down three
properties of the new behavior:

1. The connect path returns quickly even if the load would block
   forever — verified by stubbing ``load_data_type_definitions`` to
   block on a ``threading.Event``.
2. The worker thread is started, named, and marked daemon so a stuck
   load can't block app exit.
3. The connect path reports success (``self._connected`` is True) even
   when the load is still in progress; a load that ultimately fails
   does not flip the client back to a disconnected state.

Runs under ``QT_QPA_PLATFORM=offscreen`` like the other tests.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any

from PyQt6.QtWidgets import QApplication

from uaclient.uaclient import UaClient


class _StubClient:
    """Drop-in stand-in for ``asyncua.sync.Client``.

    We only need to satisfy the surface area that ``UaClient.connect``
    touches after the session is up: ``load_data_type_definitions``,
    ``load_enums``, ``load_type_definitions``, plus the attribute
    setters. The load methods are replaced per-test via
    ``load_block`` / ``load_exc`` to simulate the failure modes we
    want to exercise.
    """

    def __init__(self) -> None:
        self.application_uri: str | None = None
        self.description: str | None = None
        self.load_block = threading.Event()
        self.load_exc: BaseException | None = None
        self.load_calls: list[str] = []
        self.connect_called = threading.Event()
        self.add_listener_called = threading.Event()

    def connect(self, auto_reconnect: bool = False) -> None:
        self.connect_called.set()

    def load_data_type_definitions(self) -> None:
        self.load_calls.append("data")
        if self.load_exc is not None:
            raise self.load_exc
        self.load_block.wait()

    def load_enums(self) -> None:
        self.load_calls.append("enums")
        if self.load_exc is not None:
            raise self.load_exc
        self.load_block.wait()

    def load_type_definitions(self) -> None:
        self.load_calls.append("types")
        if self.load_exc is not None:
            raise self.load_exc
        self.load_block.wait()

    # ``_install_state_listener`` reaches into ``self.client.aio_obj.uaclient``.
    class _AioObj:
        class _UaClient:
            def _add_state_listener(self, callback: Any) -> Any:
                # Mirror the real client: returns an unsubscriber handle.
                return lambda: None

        uaclient = _UaClient()

    aio_obj = _AioObj()


class TestLoadCustomTypes(unittest.TestCase):
    def setUp(self) -> None:
        # QObject construction requires a QApplication.
        self.app = QApplication.instance() or QApplication([])
        self.uaclient = UaClient()
        self.stub = _StubClient()
        # Inject the stub where ``connect`` would normally place the
        # real sync ``Client`` — the connect path only needs the
        # ``.connect``/``.load_*``/``aio_obj`` surface.
        self.uaclient.client = self.stub

    def tearDown(self) -> None:
        # Release any blocked load so the worker thread can wind down,
        # then wait for it (with a generous bound) so a failure here
        # points at a real leak rather than a test-ordering flake.
        self.stub.load_block.set()
        thread = self.uaclient._custom_types_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self.uaclient.shutdown()

    def test_load_runs_on_daemon_worker(self) -> None:
        """``_schedule_load_custom_types`` spawns a named daemon thread."""
        self.uaclient._schedule_load_custom_types()
        thread = self.uaclient._custom_types_thread
        assert thread is not None
        self.assertTrue(thread.is_alive())
        self.assertTrue(thread.daemon, "worker must be daemon so a stuck load can't block app exit")
        self.assertEqual(thread.name, "load-custom-types")

    def test_does_not_spawn_duplicate_workers(self) -> None:
        """A second call while the first worker is alive is a no-op."""
        self.uaclient._schedule_load_custom_types()
        first = self.uaclient._custom_types_thread
        self.uaclient._schedule_load_custom_types()
        self.assertIs(self.uaclient._custom_types_thread, first)

    def test_worker_captures_client_at_call_time(self) -> None:
        """``self.client`` may be reset on disconnect before the worker
        starts; the worker should use the client it was scheduled
        with, not whatever happens to be on ``self.client`` later.
        """
        self.uaclient._schedule_load_custom_types()
        # Simulate a disconnect racing the worker: clear self.client
        # before the worker reaches the load. The worker should still
        # run the load against the captured stub.
        self.uaclient.client = None
        self.assertEqual(self.stub.load_calls, [])
        # Let the worker make progress on the first load.
        self.stub.load_block.set()
        # The worker should complete (load_calls gets all three).
        thread = self.uaclient._custom_types_thread
        assert thread is not None
        thread.join(timeout=5.0)
        self.assertEqual(self.stub.load_calls, ["data", "enums", "types"])

    def test_load_exception_does_not_propagate(self) -> None:
        """A failing load is logged, not raised — the worker must
        catch the exception and exit cleanly so the rest of the app
        is not affected.
        """
        self.stub.load_exc = RuntimeError("simulated load failure")
        # Don't block — we want the worker to run to completion (and
        # fail) on its own.
        self.uaclient._schedule_load_custom_types()
        thread = self.uaclient._custom_types_thread
        assert thread is not None
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "worker should exit after a load failure")
        # The first load raises, so the subsequent loads are not
        # attempted (the worker returns early after the exception).
        self.assertEqual(self.stub.load_calls, ["data"])
        # The connect path's own state is untouched.
        self.assertTrue(self.uaclient._connected, "_connected should stay True regardless of load outcome")

    def test_connect_returns_quickly_while_load_blocks(self) -> None:
        """``connect`` must not block on the custom-type load.

        This is the property the fix exists to guarantee: a slow or
        unresponsive server's ``load_data_type_definitions`` must not
        freeze the connect path. We measure the wall time of
        ``connect`` with a load that blocks forever.
        """
        # ``connect`` calls ``self.disconnect()`` first; that's fine
        # because self.client is currently our stub and the rest of
        # the path is mocked out below.
        self.uaclient.client = self.stub
        # The stub's ``connect`` is a no-op that records the call.
        # The state listener path is also stubbed.
        start = time.time()
        self.uaclient.connect("opc.tcp://localhost:4840")
        elapsed = time.time() - start
        # 1 s is generous; in practice the deferred-load path
        # completes in well under 50 ms.
        self.assertLess(elapsed, 1.0, f"connect() blocked for {elapsed:.2f}s on the custom-type load")
        # The session is up: stub.connect was called and the client
        # is marked connected.
        self.assertTrue(self.stub.connect_called.is_set())
        self.assertTrue(self.uaclient._connected)
        # The worker is running and will not block app exit.
        thread = self.uaclient._custom_types_thread
        assert thread is not None
        self.assertTrue(thread.is_alive())
        self.assertTrue(thread.daemon)


if __name__ == "__main__":
    unittest.main()
