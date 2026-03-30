"""Tests to boost coverage for neo4j/neptune drivers, maintenance, and CLI."""

from __future__ import annotations

import asyncio
import inspect
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


class FakeNeo4jRecord:
    def __init__(self, payload: dict):
        self._payload = payload

    def data(self) -> dict:
        return self._payload


class FakeNeo4jEagerResult:
    def __init__(self, records):
        self.records = records


class FakeNeo4jTx:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    async def run(self, query, params):
        cursor = MagicMock()
        cursor.data.return_value = [{"result": True}]
        return cursor

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class FakeNeo4jSessionForTx:
    def __init__(self):
        self.closed = False
        self._tx = FakeNeo4jTx()

    async def begin_transaction(self):
        return self._tx

    async def close(self):
        self.closed = True


class FakeNeo4jClientForDriver:
    def __init__(self, responses=None):
        self._responses = responses or [[]]
        self._call_idx = 0

    async def execute_query(self, query, parameters_, database_, **kwargs):
        records = self._responses[min(self._call_idx, len(self._responses) - 1)]
        self._call_idx += 1
        return FakeNeo4jEagerResult([FakeNeo4jRecord(r) for r in records])

    def session(self, database):
        return FakeNeo4jSessionForTx()

    async def verify_connectivity(self):
        pass

    async def close(self):
        pass


class TestNeo4jDriverExtended(unittest.IsolatedAsyncioTestCase):
    async def _make_driver(self, responses=None):
        from context_graph.backend.neo4j.config import Neo4jConfig
        from context_graph.backend.neo4j.driver import Neo4jBackendDriver

        config = Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="test", database="neo4j")
        driver = Neo4jBackendDriver(config)
        driver.client = FakeNeo4jClientForDriver(responses)
        return driver

    async def test_execute_query(self):
        driver = await self._make_driver([[{"key": "value"}]])
        result = await driver.execute_query("RETURN 1")
        self.assertEqual(result, [{"key": "value"}])

    async def test_execute_query_fallback_no_bookmark_manager(self):
        """Test fallback when bookmark_manager_ kwarg is not accepted."""
        from context_graph.backend.neo4j.config import Neo4jConfig
        from context_graph.backend.neo4j.driver import Neo4jBackendDriver

        config = Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="test", database="neo4j")
        driver = Neo4jBackendDriver(config)

        call_count = 0

        async def failing_execute(query, parameters_, database_, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and "bookmark_manager_" in kwargs:
                raise TypeError("unexpected kwarg")
            return FakeNeo4jEagerResult([FakeNeo4jRecord({"ok": True})])

        driver.client = MagicMock()
        driver.client.execute_query = failing_execute
        result = await driver.execute_query("RETURN 1")
        self.assertEqual(result, [{"ok": True}])

    async def test_session(self):
        driver = await self._make_driver()
        session = driver.session()
        self.assertIsNotNone(session)

    async def test_transaction_commit(self):
        driver = await self._make_driver()
        async with driver.transaction() as tx:
            result = await tx.run("RETURN 1")
            self.assertEqual(result, [{"result": True}])

    async def test_transaction_rollback_on_error(self):
        driver = await self._make_driver()
        with self.assertRaises(ValueError):
            async with driver.transaction() as tx:
                raise ValueError("test error")

    async def test_build_indices(self):
        driver = await self._make_driver()
        await driver.build_indices()

    async def test_build_indices_delete_existing(self):
        driver = await self._make_driver()
        await driver.build_indices(delete_existing=True)

    async def test_build_indices_ignores_equivalent_schema_error(self):
        from context_graph.backend.neo4j.config import Neo4jConfig
        from context_graph.backend.neo4j.driver import Neo4jBackendDriver

        config = Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="test", database="neo4j")
        driver = Neo4jBackendDriver(config)

        call_count = 0

        async def sometimes_fail(query, **kwargs):
            nonlocal call_count
            call_count += 1
            from neo4j.exceptions import ClientError
            if call_count == 1:
                raise ClientError("EquivalentSchemaRuleAlreadyExists")
            return FakeNeo4jEagerResult([])

        driver.execute_query = sometimes_fail
        await driver.build_indices()

    async def test_health_check(self):
        driver = await self._make_driver()
        await driver.health_check()

    async def test_close(self):
        driver = await self._make_driver()
        await driver.close()

    async def test_capabilities(self):
        driver = await self._make_driver()
        self.assertTrue(driver.capabilities.supports_transactions)
        self.assertTrue(driver.capabilities.supports_lexical_search)
        self.assertFalse(driver.capabilities.supports_vector_search)

    async def test_properties(self):
        driver = await self._make_driver()
        self.assertIsNotNone(driver.search)
        self.assertIsNotNone(driver.mutations)
        self.assertIsNotNone(driver.maintenance)
        self.assertIsNotNone(driver.traversal)


class TestNeo4jBackendSession(unittest.IsolatedAsyncioTestCase):
    async def test_session_run(self):
        from context_graph.backend.neo4j.driver import Neo4jBackendSession

        fake_session = MagicMock()
        cursor = MagicMock()
        cursor.data.return_value = [{"key": "val"}]
        fake_session.run = AsyncMock(return_value=cursor)
        fake_session.close = AsyncMock()

        session = Neo4jBackendSession(fake_session)
        result = await session.run("RETURN 1")
        self.assertEqual(result, [{"key": "val"}])
        await session.close()

    async def test_session_run_awaitable_data(self):
        from context_graph.backend.neo4j.driver import Neo4jBackendSession

        fake_session = MagicMock()

        async def fake_data():
            return [{"key": "val"}]

        cursor = MagicMock()
        cursor.data.return_value = fake_data()
        fake_session.run = AsyncMock(return_value=cursor)
        fake_session.close = AsyncMock()

        session = Neo4jBackendSession(fake_session)
        result = await session.run("RETURN 1")
        self.assertEqual(result, [{"key": "val"}])

    async def test_session_aexit(self):
        from context_graph.backend.neo4j.driver import Neo4jBackendSession

        fake_session = MagicMock()
        fake_session.close = AsyncMock()
        session = Neo4jBackendSession(fake_session)
        await session.__aexit__(None, None, None)
        fake_session.close.assert_awaited_once()

    async def test_session_execute_write(self):
        from context_graph.backend.neo4j.driver import Neo4jBackendSession

        fake_session = MagicMock()
        fake_session.execute_write = AsyncMock(return_value="result")
        session = Neo4jBackendSession(fake_session)
        result = await session.execute_write(lambda s: "done")
        self.assertEqual(result, "result")


class TestNeptuneDriverExtended(unittest.IsolatedAsyncioTestCase):
    def _make_driver(self):
        from context_graph.backend.neptune.config import NeptuneConfig
        from context_graph.backend.neptune.driver import NeptuneBackendDriver

        config = NeptuneConfig(host="neptune-graph://test-graph-id")

        fake_client = MagicMock()
        fake_client.client = MagicMock()
        fake_client.client.execute_open_cypher_query.return_value = {"results": []}

        with patch(
            "context_graph.backend.neptune.driver.NeptuneAnalyticsGraph",
            return_value=fake_client,
        ), patch(
            "context_graph.backend.neptune.driver.NeptuneGraph",
            MagicMock(),
        ):
            driver = NeptuneBackendDriver(config)
        return driver

    async def test_execute_query(self):
        driver = self._make_driver()
        result = await driver.execute_query("RETURN 1")
        self.assertEqual(result, [])

    async def test_session(self):
        driver = self._make_driver()
        session = driver.session()
        self.assertIsNotNone(session)

    async def test_close_with_close_method(self):
        driver = self._make_driver()
        driver.client.client.close = MagicMock()
        await driver.close()

    async def test_close_without_close_method(self):
        driver = self._make_driver()
        driver.client.client = MagicMock(spec=[])
        await driver.close()

    async def test_build_indices_no_aoss(self):
        driver = self._make_driver()
        driver.aoss_client = None
        await driver.build_indices()

    async def test_health_check(self):
        driver = self._make_driver()
        await driver.health_check()

    async def test_capabilities(self):
        driver = self._make_driver()
        self.assertFalse(driver.capabilities.supports_transactions)
        self.assertTrue(driver.capabilities.supports_graph_traversal)

    async def test_properties(self):
        driver = self._make_driver()
        self.assertIsNotNone(driver.search)
        self.assertIsNotNone(driver.mutations)
        self.assertIsNotNone(driver.maintenance)
        self.assertIsNotNone(driver.traversal)


class TestNeptuneBackendSession(unittest.IsolatedAsyncioTestCase):
    async def test_session_run(self):
        from context_graph.backend.neptune.driver import NeptuneBackendDriver, NeptuneBackendSession

        fake_driver = MagicMock()
        fake_driver.execute_query = AsyncMock(return_value=[{"ok": True}])
        session = NeptuneBackendSession(fake_driver)
        result = await session.run("RETURN 1")
        self.assertEqual(result, [{"ok": True}])

    async def test_session_close(self):
        from context_graph.backend.neptune.driver import NeptuneBackendSession

        fake_driver = MagicMock()
        session = NeptuneBackendSession(fake_driver)
        result = await session.close()
        self.assertIsNone(result)

    async def test_session_aexit(self):
        from context_graph.backend.neptune.driver import NeptuneBackendSession

        fake_driver = MagicMock()
        session = NeptuneBackendSession(fake_driver)
        await session.__aexit__(None, None, None)

    async def test_session_execute_write(self):
        from context_graph.backend.neptune.driver import NeptuneBackendSession

        fake_driver = MagicMock()
        session = NeptuneBackendSession(fake_driver)

        async def writer(s):
            return "written"

        result = await session.execute_write(writer)
        self.assertEqual(result, "written")


class TestNeptuneDriverConfigErrors(unittest.TestCase):
    def test_empty_host_raises(self):
        from context_graph.backend.neptune.config import NeptuneConfig
        from context_graph.backend.neptune.driver import NeptuneBackendDriver
        from context_graph.backend.errors import BackendConfigurationError

        config = NeptuneConfig(host="")
        with self.assertRaises(BackendConfigurationError):
            NeptuneBackendDriver(config)

    def test_invalid_host_scheme_raises(self):
        from context_graph.backend.neptune.config import NeptuneConfig
        from context_graph.backend.neptune.driver import NeptuneBackendDriver
        from context_graph.backend.errors import BackendConfigurationError

        config = NeptuneConfig(host="http://invalid")
        with patch(
            "context_graph.backend.neptune.driver.NeptuneGraph", MagicMock(),
        ), patch(
            "context_graph.backend.neptune.driver.NeptuneAnalyticsGraph", MagicMock(),
        ):
            with self.assertRaises(BackendConfigurationError):
                NeptuneBackendDriver(config)


if __name__ == "__main__":
    unittest.main()
