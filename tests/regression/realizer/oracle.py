"""Loader for the frozen single-file Realizer oracle in ``legacy/realizer_single_file/``.

The oracle is the v1.0.0 single-file Realizer kept verbatim as the behavioral reference for the modular rewrite
(docs/architecture/realizer-repo-reorg-design.md, section 21.4). It imports its sibling packages by top-level name
(``from contracts import ...``, ``from reporters import ...``), and its engine module is named ``realizer``, the
same name as the future ``src/realizer`` package. Putting its directory on ``sys.path`` would therefore shadow, or be
shadowed by, other top-level names.

Instead the oracle is loaded once under the private package name ``legacy_realizer_single_file``: its ``contracts``
package becomes ``legacy_realizer_single_file.contracts`` and its engine ``legacy_realizer_single_file.realizer``.
The top-level ``contracts`` (and ``reporters``) names are aliased only while the oracle's own modules execute their
absolute imports, then restored, so nothing outside the oracle can see them.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Dict, Iterable

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

ORACLE_DIR = ROOT / "legacy" / "realizer_single_file"
PACKAGE = "legacy_realizer_single_file"
CONTRACT_MODULES = ("realizer_contract", "realizer_schema", "realizer_workbook_contract", "reporter_contract")


class _TopLevelAlias:
    """Temporarily map top-level names (``contracts``, ``reporters``) to the oracle's private packages."""

    def __init__(self, names: Iterable[str]):
        self.names = tuple(names)
        self.saved: Dict[str, types.ModuleType] = {}

    def __enter__(self) -> None:
        for name in self.names:
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                self.saved[key] = sys.modules.pop(key)
            sys.modules[name] = importlib.import_module(f"{PACKAGE}.{name}")

    def __exit__(self, *exc) -> None:
        for name in self.names:
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                del sys.modules[key]
        sys.modules.update(self.saved)


def _package() -> types.ModuleType:
    """The private parent package whose ``__path__`` is the oracle directory."""
    pkg = sys.modules.get(PACKAGE)
    if pkg is None:
        pkg = types.ModuleType(PACKAGE)
        pkg.__path__ = [str(ORACLE_DIR)]
        pkg.__package__ = PACKAGE
        pkg.__file__ = None
        sys.modules[PACKAGE] = pkg
    return pkg


def contracts() -> types.ModuleType:
    """The oracle's ``contracts`` package with its submodules imported (``.realizer_contract`` etc.)."""
    _package()
    pkg = importlib.import_module(f"{PACKAGE}.contracts")
    for name in CONTRACT_MODULES:
        importlib.import_module(f"{PACKAGE}.contracts.{name}")
    return pkg


def engine() -> types.ModuleType:
    """The oracle engine (``realizer.py``): ``realize``, ``demo_observation``, ``load_ledger``, ..."""
    key = f"{PACKAGE}.realizer"
    if key in sys.modules:
        return sys.modules[key]
    contracts()
    with _TopLevelAlias(("contracts",)):
        return importlib.import_module(key)


def reporters_available() -> bool:
    """Whether the workbook renderers can load: they need the proprietary ``artifact_tool`` runtime."""
    return importlib.util.find_spec("artifact_tool") is not None


def reporters() -> types.ModuleType:
    """The oracle's workbook renderers (``reporters.realizer_workbook_reporter``); requires ``artifact_tool``."""
    key = f"{PACKAGE}.reporters.realizer_workbook_reporter"
    if key in sys.modules:
        return sys.modules[key]
    contracts()
    _package()
    importlib.import_module(f"{PACKAGE}.reporters")
    with _TopLevelAlias(("contracts", "reporters")):
        importlib.import_module(f"{PACKAGE}.reporters.workbook_reporter")
        return importlib.import_module(key)


def rc() -> types.ModuleType:
    """``contracts.realizer_contract`` of the oracle."""
    contracts()
    return sys.modules[f"{PACKAGE}.contracts.realizer_contract"]


def realizer_schema() -> types.ModuleType:
    contracts()
    return sys.modules[f"{PACKAGE}.contracts.realizer_schema"]


def realizer_workbook_contract() -> types.ModuleType:
    contracts()
    return sys.modules[f"{PACKAGE}.contracts.realizer_workbook_contract"]
