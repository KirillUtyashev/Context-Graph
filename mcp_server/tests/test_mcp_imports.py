from __future__ import annotations

import importlib
import pkgutil
import unittest

import mcp_server.src as src


class McpImportSmokeTests(unittest.TestCase):
    def test_import_all_modules_under_package(self) -> None:
        imported_modules = []
        for module_info in pkgutil.walk_packages(
            src.__path__,
            src.__name__ + ".",
        ):
            imported_modules.append(importlib.import_module(module_info.name).__name__)

        self.assertIn("mcp_server.src.app", imported_modules)
        self.assertIn("mcp_server.src.bootstrap", imported_modules)
        self.assertIn("mcp_server.src.provenance.search", imported_modules)
        self.assertIn("mcp_server.src.tools.provenance", imported_modules)
