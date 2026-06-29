# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- `make` — regenerates everything generated from Qt sources. `pyuic6` for the `.ui` files and `pyside6-rcc` for `uawidgets/resources.qrc`. The Makefile rewrites the rcc output's `from PySide6` import to `from PyQt6` (PyQt6 dropped `pyrcc6`; the binary resource format is framework-agnostic).
- `make run` — launches the app via `python3 app.py`.
- `make edit` — opens `mainwindow_ui.ui` in Qt Creator.
- `uv sync` — install runtime + dev deps into `.venv` (project uses [uv](https://docs.astral.sh/uv/) for env/build management).
- `uv run python app.py` — launch the app from the venv.
- `uv run mypy uaclient uawidgets` — type check. `pyproject.toml` enables `strict = true` for our packages; generated `*_ui.py` / `breeze_resources.py` / `resources.py` are excluded.
- `uv run python tests.py` — runs the unittest suite. Tests spin up a real `asyncua` server on `opc.tcp://localhost:48400/freeopcua/server/` (and `:48401` for the reconnect test) and drive the live `Window`, so a display is required. Headless runs work with `QT_QPA_PLATFORM=offscreen`. Run a single test with `python3 tests.py TestClient.test_select_objects`. Two test classes exist: `TestClient` (browse + attribute/refs assertions) and `TestAutoReconnect` (verifies widgets grey out and a banner appears when the server dies, then re-enable on reconnect).
- `uv run python release.py` — interactive: prompts before commit, push, and publish. Bumps `version` in `pyproject.toml`, tags, runs `uv build` + `uv publish`.

## Architecture

This is a PyQt6 desktop OPC-UA client. The split between "Qt glue" and "OPC-UA logic" matters when making changes:

- **`app.py`** — entry point; delegates to `uaclient.mainwindow.main`. The `opcua-client` console script in `pyproject.toml` (`[project.scripts]`) points at the same `main`.
- **`uaclient/uaclient.py`** — `UaClient` is the only place that talks to `asyncua`. It wraps `asyncua.sync.Client` so the rest of the app stays on the Qt main thread. It owns a long-lived `ThreadLoop` shared across every `Client` it opens (sync.Client stops its own threadloop on disconnect, which would tear down the asyncio loop our session lives on; passing our own keeps it alive across connect/disconnect cycles). Security settings (mode, policy, user cert/key, application cert/key) are persisted per-URI via `QSettings`. Any new server-side capability should land here, not in the UI.
- **`uawidgets/`** — sibling package of `uaclient/`. Holds the reusable Qt widgets (`TreeWidget`, `AttrsWidget`, `RefsWidget`, the call-method dialog, the `QtHandler` log sink, and the `trycatchslot` decorator). Used to live in a separate `opcua-widgets` repo on PyPI; folded back into this project once the GUI was the only consumer.
- **`uaclient/mainwindow.py`** — the `Window` (a `QMainWindow`) wires `UaClient` to the widgets from `uawidgets`. Local sub-UIs live in this file as plain classes:
  - `DataChangeUI` / `DataChangeHandler` — subscription model for variable values.
  - `EventUI` / `EventHandler` — subscription model for events.
  - Both handlers emit `pyqtSignal`s with `Qt.QueuedConnection` so notifications arriving on the asyncua background thread are marshaled back to the Qt thread before touching models.
- **`uaclient/graphwidget.py`** — pyqtgraph-based live plot of subscribed variables. Soft-imports pyqtgraph/numpy and falls back to a placeholder label if they're missing.
- **`uaclient/connection_dialog.py`** + **`application_certificate_dialog.py`** — per-connection security config and application certificate config. Their `*_ui.py` siblings are generated; edit the `.ui` files and re-run `make`.
- **`uaclient/theme/`** — Breeze qrc-compiled stylesheets (`breeze_resources.py`); dark mode is toggled by `QSettings["dark_mode"]` and requires a restart (`Window.dark_mode` shows a "Restart for changes to take effect" dialog).
- **`connection/`** — a separate Qt/C++ implementation of the connection dialog. Not built into the Python app; treat as reference only unless you're rebuilding that project.
- **`snap/snapcraft.yaml`** — Snap packaging config (Qt5-based, currently stale; not built by the default workflow).

### Threading model — read this before touching subscriptions or the reconnect path

`asyncua.sync.Client` runs the OPC-UA protocol on a background thread. The Qt UI lives on the main thread. The pattern is:

- Slots in `Window` and sub-UIs that need to mutate models must run on the GUI thread.
- The two `*Handler` classes above marshal asyncua notifications back via `pyqtSignal` + `Qt.QueuedConnection`.
- `UaClient._install_state_listener` registers a sync callback on the asyncua thread; the callback only does `connection_state_changed.emit(state.value)`, and the GUI connects with `QueuedConnection` so `_on_connection_state_changed` runs on the GUI thread.
- `Window._apply_ui_state` drives buttons/views/banner from the connection state: `idle`, `connected`, or `reconnecting`. When `client.connect(auto_reconnect=True)` is used (it is, in `UaClient.connect`), the supervisor transparently re-establishes the session; the UI should grey out views and show a banner until the state transitions back to `connected`. On user-initiated `disconnect()`, the state listener is unhooked first so the transitional `disconnecting`/`disconnected` events don't trigger a spurious "auto-reconnect" banner.

### Generated files — do not hand-edit

`uaclient/mainwindow_ui.py`, `uaclient/connection_ui.py`, `uaclient/applicationcertificate_ui.py`, `uaclient/theme/breeze_resources.py`, and `uawidgets/resources.py` are produced by `make` from `.ui` / `.qrc` sources. Edit the source, then regenerate. `mypy` is configured to skip these.

### State persistence

The app stores virtually all user state in `QSettings` under organization `FreeOpcUa`, application `OpcUaClient`: address history (`address_list`), last-browsed node per URI (`current_node`), window geometry/state, dark mode, per-URI security settings, and per-widget header state. When changing settings keys, search for the string usage in `uaclient.py` and `mainwindow.py` together.

### External dependencies

Runtime deps are `asyncua` (>=2.0, needed for the auto-reconnect supervisor and Python 3.14 compatibility), `PyQt6`, `pyqtgraph`, and `numpy`. Dev dep: `mypy`. `asyncua` is currently consumed via a `[tool.uv.sources]` path source pointing at `../opcua-asyncio` because the 2.x line is still in pre-release; drop the source entry once `asyncua>=2.0` is on PyPI.
