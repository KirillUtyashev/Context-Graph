from __future__ import annotations

import importlib
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.app as app_module
import src.bootstrap as bootstrap
import src.config.cli as cli_module
import src.config.settings as settings_module
from src.state import ServerState
from src.tools import common as common_tools
from src.tools.health import register_health_routes


class FakeFastMCP:
    def __init__(self, name: str, *, instructions: str) -> None:
        self.name = name
        self.instructions = instructions
        self.tools: list[SimpleNamespace] = []
        self.routes: list[SimpleNamespace] = []
        self.settings = SimpleNamespace(host="0.0.0.0", port=8000, transport_security=None)

    def tool(self, name: str | None = None, **_kwargs):
        def decorator(fn):
            self.tools.append(SimpleNamespace(name=name or fn.__name__, fn=fn))
            return fn

        return decorator

    def custom_route(self, path: str, methods: list[str]):
        def decorator(fn):
            self.routes.append(SimpleNamespace(path=path, methods=tuple(methods), fn=fn))
            return fn

        return decorator


class FakeMetadataSnapshotService:
    def __init__(
        self,
        *,
        ready: bool,
        metadata_store: object | None = None,
        metadata_init_error: str | None = None,
    ) -> None:
        self._ready = ready
        self.metadata_store = metadata_store
        self.metadata_init_error = metadata_init_error

    def initialize_metadata_store(self) -> bool:
        return self._ready


def test_main_module_exits_with_bootstrap_return_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.bootstrap.main", lambda: 23)

    with pytest.raises(SystemExit) as exc:
        runpy.run_module("src.__main__", run_name="__main__")

    assert exc.value.code == 23


def test_build_app_and_get_default_app_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    app = app_module.build_app(fastmcp_cls=FakeFastMCP)
    assert {tool.name for tool in app.tools} == {
        "search_code",
        "get_code_graph_schema",
        "search_code_graph",
        "trace_call_path",
        "get_code_snippet",
        "search_context",
        "get_node_neighborhood",
        "get_timeline",
        "get_code_context",
        "record_agent_observation",
    }

    build_calls: list[object] = []

    def fake_build_app(state):
        build_calls.append(state)
        return {"state": state}

    monkeypatch.setattr(app_module, "_default_app", None)
    monkeypatch.setattr(app_module, "build_app", fake_build_app)
    cached_first = app_module.get_default_app()
    cached_second = app_module.get_default_app()

    assert cached_first == cached_second
    assert build_calls == [app_module.DEFAULT_STATE]


@pytest.mark.asyncio
async def test_register_health_routes_returns_json_payload() -> None:
    app = FakeFastMCP("demo", instructions="")
    register_health_routes(app)

    assert len(app.routes) == 1
    response = await app.routes[0].fn(None)

    assert response.status_code == 200
    assert response.body == b'{"status":"healthy","service":"context-graph-mcp"}'


def test_server_state_require_helpers_raise_meaningful_errors() -> None:
    state = ServerState()

    with pytest.raises(RuntimeError, match="configuration is not initialized"):
        state.require_config()
    with pytest.raises(RuntimeError, match="backend runtime is not configured"):
        state.require_backend_runtime()
    with pytest.raises(RuntimeError, match="Metadata snapshot service is not configured"):
        state.require_metadata_snapshot_service()


def test_common_normalization_helpers_cover_invalid_and_deduped_inputs() -> None:
    assert common_tools._normalize_repo_file_path("/tmp", None) is None
    assert common_tools._normalize_repo_file_path("/tmp", "file.py") == Path("/tmp/file.py").resolve()
    assert common_tools._current_timestamp().endswith("Z")
    assert common_tools.error_response("oops") == {"error": "oops"}
    assert (
        common_tools._supported_values_error("kind", ("a", "b"))
        == "kind must be one of: a, b"
    )
    assert (
        common_tools._normalize_supported_value(
            " Function ",
            parameter_name="target_kind",
            supported_values=("function", "class"),
        )
        == "function"
    )
    assert (
        common_tools._normalize_supported_list(
            [" claims ", "claims", "code"],
            parameter_name="sources",
            supported_values=("claims", "code"),
        )
        == ["claims", "code"]
    )
    assert common_tools._normalize_supported_list(
        None,
        parameter_name="sources",
        supported_values=("claims",),
    ) is None

    with pytest.raises(common_tools.ParameterValidationError, match="target_kind must be one of"):
        common_tools._normalize_supported_value(
            "widget",
            parameter_name="target_kind",
            supported_values=("function", "class"),
        )
    with pytest.raises(common_tools.ParameterValidationError, match="sources must only contain"):
        common_tools._normalize_supported_list(
            ["claims", "widgets"],
            parameter_name="sources",
            supported_values=("claims", "code"),
        )


@pytest.mark.asyncio
async def test_get_backend_runtime_and_metadata_snapshot_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = SimpleNamespace(ensure_initialized=AsyncMock())
    state = ServerState(backend_runtime=runtime)
    assert await common_tools._get_backend_runtime(state) is runtime
    runtime.ensure_initialized.assert_awaited_once()

    fake_store = object()
    snapshot = {"repo_id": "demo"}
    service = FakeMetadataSnapshotService(ready=True, metadata_store=fake_store)
    monkeypatch.setattr(common_tools.code_graph_queries, "load_latest_snapshot", lambda store, repo_id: snapshot)
    loaded = await common_tools._get_metadata_snapshot(
        ServerState(metadata_snapshot_service=service),
        "demo",
    )
    assert loaded is snapshot


@pytest.mark.asyncio
async def test_get_metadata_snapshot_helper_reports_unavailable_and_missing_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable = ServerState(
        metadata_snapshot_service=FakeMetadataSnapshotService(
            ready=False,
            metadata_store=None,
            metadata_init_error="db offline",
        )
    )
    with pytest.raises(common_tools.code_graph_queries.CodeGraphQueryError, match="db offline"):
        await common_tools._get_metadata_snapshot(unavailable, "demo")

    service = FakeMetadataSnapshotService(ready=True, metadata_store=object())
    monkeypatch.setattr(common_tools.code_graph_queries, "load_latest_snapshot", lambda store, repo_id: None)
    with pytest.raises(common_tools.code_graph_queries.CodeGraphQueryError, match="No indexed snapshot found"):
        await common_tools._get_metadata_snapshot(
            ServerState(metadata_snapshot_service=service),
            "demo",
        )


def test_cli_load_server_config_sets_env_and_applies_small_model_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(tmp_path / "mcp.yaml"),
            "--transport",
            "stdio",
            "--small-model",
            "gpt-small",
        ]
    )
    config = cli_module.load_server_config(args)

    assert config.server.transport == "stdio"
    assert config.llm.small_model == "gpt-small"
    assert os.environ["CONFIG_PATH"] == str(tmp_path / "mcp.yaml")


def test_yaml_settings_source_expands_false_and_lists(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "server:\n"
        "  host: ${HOST:127.0.0.1}\n"
        "flags:\n"
        "  enabled: ${FEATURE:false}\n"
        "values:\n"
        "  - ${FIRST:one}\n"
        "  - ${SECOND:false}\n",
        encoding="utf-8",
    )
    source = settings_module.YamlSettingsSource(settings_module.MCPServerConfig, config_path)

    with pytest.MonkeyPatch.context() as env_patch:
        env_patch.setenv("HOST", "0.0.0.0")
        loaded = source()

    assert loaded["server"]["host"] == "0.0.0.0"
    assert loaded["flags"]["enabled"] is False
    assert loaded["values"] == ["one", False]


def test_bootstrap_reload_uses_default_dotenv_when_env_file_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_load_dotenv(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr("dotenv.load_dotenv", fake_load_dotenv)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    importlib.reload(bootstrap)

    assert calls
    assert calls[0][0] == ()


def test_initialize_server_uses_default_app_and_transport_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTransportSecuritySettings:
        def __init__(self, *, enable_dns_rebinding_protection: bool) -> None:
            self.enable_dns_rebinding_protection = enable_dns_rebinding_protection

    state = ServerState()
    app = FakeFastMCP("demo", instructions="")
    monkeypatch.setattr(bootstrap, "get_default_app", lambda: app)
    monkeypatch.setitem(
        sys.modules,
        "mcp.server.transport_security",
        SimpleNamespace(TransportSecuritySettings=FakeTransportSecuritySettings),
    )

    server_config = bootstrap.initialize_server(["--transport", "http"], state=state, mcp_app=None)

    assert server_config.transport == "http"
    assert state.backend_runtime is not None
    assert state.metadata_snapshot_service is not None
    assert isinstance(app.settings.transport_security, FakeTransportSecuritySettings)
    assert app.settings.transport_security.enable_dns_rebinding_protection is False


@pytest.mark.asyncio
async def test_run_mcp_server_rejects_unsupported_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FakeFastMCP("demo", instructions="")
    monkeypatch.setattr(bootstrap, "initialize_server", lambda *args, **kwargs: SimpleNamespace(transport="weird"))

    with pytest.raises(ValueError, match='Unsupported transport: weird'):
        await bootstrap.run_mcp_server([], mcp_app=app)


def test_bootstrap_main_handles_interrupt_and_reraises_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_keyboard_interrupt(coro):
        coro.close()
        raise KeyboardInterrupt()

    def raise_value_error(coro):
        coro.close()
        raise ValueError("boom")

    monkeypatch.setattr(bootstrap.asyncio, "run", raise_keyboard_interrupt)
    bootstrap.main([])

    monkeypatch.setattr(bootstrap.asyncio, "run", raise_value_error)
    with pytest.raises(ValueError, match="boom"):
        bootstrap.main([])
