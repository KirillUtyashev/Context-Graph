from __future__ import annotations

from types import SimpleNamespace
import unittest

import src.bootstrap as bootstrap
from src.state import ServerState


class FakeMCPApp:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(host="0.0.0.0", port=8000, transport_security=None)
        self.calls: list[str] = []

    async def run_stdio_async(self) -> None:
        self.calls.append("stdio")

    async def run_sse_async(self) -> None:
        self.calls.append("sse")

    async def run_streamable_http_async(self) -> None:
        self.calls.append("http")


class FakeBackendRuntime:
    def __init__(self, config, *, semaphore_limit: int) -> None:
        self.config = config
        self.semaphore_limit = semaphore_limit


class FakeMetadataSnapshotService:
    def __init__(self, config) -> None:
        self.config = config


class BootstrapTests(unittest.TestCase):
    def test_main_help_raises_system_exit_before_runtime_boot(self) -> None:
        with self.assertRaises(SystemExit) as exc:
            bootstrap.main(["--help"])

        self.assertEqual(exc.exception.code, 0)

    def test_initialize_server_applies_cli_settings(self) -> None:
        state = ServerState()
        app = FakeMCPApp()

        server_config = bootstrap.initialize_server(
            ["--transport", "stdio", "--host", "127.0.0.1", "--port", "9123", "--group-id", "cg"],
            state=state,
            mcp_app=app,
            backend_runtime_cls=FakeBackendRuntime,
            metadata_snapshot_service_cls=FakeMetadataSnapshotService,
        )

        self.assertEqual(server_config.transport, "stdio")
        self.assertIsNotNone(state.config)
        self.assertIsNotNone(state.backend_runtime)
        self.assertIsNotNone(state.metadata_snapshot_service)
        self.assertEqual(state.config.namespace.group_id, "cg")
        self.assertEqual(app.settings.host, "127.0.0.1")
        self.assertEqual(app.settings.port, 9123)


class BootstrapTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_mcp_server_dispatches_stdio(self) -> None:
        state = ServerState()
        app = FakeMCPApp()

        await bootstrap.run_mcp_server(
            ["--transport", "stdio", "--host", "127.0.0.1"],
            state=state,
            mcp_app=app,
            backend_runtime_cls=FakeBackendRuntime,
            metadata_snapshot_service_cls=FakeMetadataSnapshotService,
        )

        self.assertEqual(app.calls, ["stdio"])

    async def test_run_mcp_server_dispatches_sse(self) -> None:
        state = ServerState()
        app = FakeMCPApp()

        await bootstrap.run_mcp_server(
            ["--transport", "sse", "--host", "127.0.0.1"],
            state=state,
            mcp_app=app,
            backend_runtime_cls=FakeBackendRuntime,
            metadata_snapshot_service_cls=FakeMetadataSnapshotService,
        )

        self.assertEqual(app.calls, ["sse"])

    async def test_run_mcp_server_dispatches_http(self) -> None:
        state = ServerState()
        app = FakeMCPApp()

        await bootstrap.run_mcp_server(
            ["--transport", "http", "--host", "127.0.0.1"],
            state=state,
            mcp_app=app,
            backend_runtime_cls=FakeBackendRuntime,
            metadata_snapshot_service_cls=FakeMetadataSnapshotService,
        )

        self.assertEqual(app.calls, ["http"])
