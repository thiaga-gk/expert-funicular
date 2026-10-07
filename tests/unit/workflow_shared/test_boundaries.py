"""Shared package boundaries (src/workflow_shared): it imports neither product and its Databricks access is read-only."""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

SHARED = ROOT / "src" / "workflow_shared"
PRODUCTS = ("publisher", "realizer")
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def imported_modules(path):
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.paths = sorted(SHARED.rglob("*.py"))

    def test_the_package_exists_with_its_access_and_vocabulary_modules(self):
        names = {p.relative_to(SHARED).as_posix() for p in self.paths}
        self.assertLessEqual({"__init__.py", "databricks/__init__.py", "databricks/access.py", "databricks/vocabulary.py"}, names)

    def test_no_module_imports_a_product(self):
        for path in self.paths:
            for module in imported_modules(path):
                self.assertNotIn(module.split(".")[0], PRODUCTS, f"{path.relative_to(SHARED)} imports {module}")

    def test_no_mutating_http_method_appears(self):
        for path in self.paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn(node.value.strip().upper(), MUTATING_METHODS, f"{path.relative_to(SHARED)}:{node.lineno}")

    def test_every_http_request_is_an_explicit_get(self):
        requests = 0
        for path in self.paths:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Request":
                    requests += 1
                    method = {k.arg: k.value for k in node.keywords}.get("method")
                    self.assertIsInstance(method, ast.Constant, f"{path.relative_to(SHARED)}:{node.lineno}")
                    self.assertEqual(method.value, "GET")
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"HTTPConnection", "HTTPSConnection"}, f"{path.relative_to(SHARED)}:{node.lineno}")
        self.assertEqual(requests, 1)


if __name__ == "__main__":
    unittest.main()
