"""Configuration loading: resolve the execution mode and the run scope a request runs under."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.config.defaults import SCOPE_SCENARIOS
from publisher.config.settings import _SCOPE_SCHEMA_PATH, EXECUTION_MODES
from publisher.contracts.scope import FULL_SCOPE, WAREHOUSE_REF_PATTERN, RunScope, WarehouseSelector


def resolve_execution_mode(mode: str) -> str:
    """The execution mode a run uses. Only local execution exists today; other modes (e.g. a Databricks Asset Bundle
    job) resolve here to their adapters when they are built.

    Raises PublisherError (CLI exit status 2) for a mode outside EXECUTION_MODES. The mode is a non-sensitive label.
    """
    if mode not in EXECUTION_MODES:
        raise PublisherError(f"Unsupported execution mode {mode!r}; supported: {', '.join(EXECUTION_MODES)}")
    LOGGER.debug("config.execution_mode mode=%s", mode)
    return mode


def _scope_names() -> str:
    """The accepted ``--scope`` values, for error messages."""
    return "; ".join(f"{s}[:{','.join(n.replace('_', '-') for n in t)}]" for s, (_, t) in SCOPE_SCENARIOS.items())


def _knobs_from_specs(specs: Sequence[str]) -> Tuple[str, ...]:
    """Knobs from ``--scope`` values: ``scenario`` (all its techniques) or ``scenario:technique[,technique...]``.

    Technique names accept hyphens or underscores. Unknown names, an empty technique list and a repeated scenario
    raise PublisherError.
    """
    knobs: Set[str] = set()
    seen: Set[str] = set()
    for raw in specs:
        scenario, sep, rest = str(raw).strip().partition(":")
        scenario = scenario.strip().lower()
        if scenario not in SCOPE_SCENARIOS:
            raise PublisherError(f"Unknown scope scenario {scenario!r} in --scope {raw!r}; use {_scope_names()}")
        if scenario in seen:
            raise PublisherError(f"Scope scenario {scenario!r} is given more than once; list its techniques in one --scope")
        seen.add(scenario)
        techniques = SCOPE_SCENARIOS[scenario][1]
        if not sep:
            knobs.update(techniques.values())
            continue
        names = [x.strip().lower().replace("-", "_") for x in rest.split(",")]
        if not any(names) or not all(names):
            raise PublisherError(f"--scope {raw!r} needs technique names after ':'; use {_scope_names()}")
        for name in names:
            if name not in techniques:
                raise PublisherError(f"Unknown technique {name!r} for scope scenario {scenario!r}; use {_scope_names()}")
            knobs.add(techniques[name])
    return tuple(sorted(knobs))


def _selectors(values: Sequence[str]) -> Tuple[WarehouseSelector, ...]:
    """Warehouse selectors from ``all`` or ``<warehouse_id>`` / ``<workspace_id>/<warehouse_id>`` values (sorted, unique).

    ``all`` (or nothing) selects every warehouse; ``all`` mixed with IDs, or a malformed value, raises PublisherError.
    """
    cleaned = [str(v).strip() for v in values]
    if not cleaned or cleaned == ["all"]:
        return ()
    if "all" in cleaned:
        raise PublisherError("--warehouse all cannot be combined with specific warehouse IDs")
    out: Set[WarehouseSelector] = set()
    for value in cleaned:
        if not re.match(WAREHOUSE_REF_PATTERN, value):
            raise PublisherError(f"Invalid warehouse selector {value!r}; use <warehouse_id> or <workspace_id>/<warehouse_id>")
        workspace, sep, warehouse = value.rpartition("/")
        out.add(WarehouseSelector(warehouse, workspace) if sep else WarehouseSelector(value))
    return tuple(sorted(out, key=lambda s: (s.warehouse_id, s.workspace_id or "")))


def _load_scope_file(path: str) -> Tuple[Tuple[str, ...], List[str]]:
    """(knobs, warehouse values) from a ``--scope-file``, checked against schema/run-scope.schema.json.

    Raises PublisherError for an unreadable or invalid file, an enabled scenario with no technique and a file that
    enables nothing. A disabled scenario's technique toggles are ignored.
    """
    try:
        payload: Any = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublisherError(f"{path}: cannot read the scope file ({type(exc).__name__})") from exc
    try:
        import jsonschema  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("jsonschema>=4.23 is required to validate scope files") from exc
    schema = json.loads(_SCOPE_SCHEMA_PATH.read_text(encoding="utf-8"))
    error = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(schema).iter_errors(payload))
    if error is not None:
        where = "/".join(str(x) for x in error.absolute_path) or "(root)"
        raise PublisherError(f"{path}: invalid scope file at {where}: {error.message}")
    knobs: Set[str] = set()
    scenarios: Dict[str, Any] = payload["scenarios"]
    for scenario, (_, techniques) in SCOPE_SCENARIOS.items():
        block = scenarios[scenario]
        if not block["enabled"]:
            continue
        chosen = [knob for name, knob in techniques.items() if block["techniques"][name]]
        if not chosen:
            raise PublisherError(f"{path}: scenario {scenario!r} is enabled but no technique is turned on")
        knobs.update(chosen)
    if not knobs:
        raise PublisherError(f"{path}: no scenario is enabled")
    warehouses = payload["warehouses"]
    if warehouses != "all" and "all" in warehouses:
        raise PublisherError(f'{path}: use "warehouses": "all" for every warehouse, not a list containing "all"')
    return tuple(sorted(knobs)), (["all"] if warehouses == "all" else list(warehouses))


def resolve_scope(scope_specs: Sequence[str] = (), scope_file: Optional[str] = None, warehouses: Sequence[str] = ()) -> RunScope:
    """The run scope from ``--scope`` values or a ``--scope-file``, and ``--warehouse`` values.

    No input is the full scope. ``--scope-file`` holds the warehouses too, so it cannot be combined with ``--scope``
    or ``--warehouse``. Raises PublisherError (CLI exit status 2) for any invalid input; whether a selected warehouse
    exists is checked by the Governor once the evidence is known.
    """
    if scope_file is not None:
        if scope_specs or warehouses:
            raise PublisherError("--scope-file cannot be combined with --scope or --warehouse (the file holds both)")
        knobs, values = _load_scope_file(scope_file)
        scope = RunScope(knobs=knobs, warehouses=_selectors(values))
    else:
        scope = RunScope(
            knobs=_knobs_from_specs(scope_specs) if scope_specs else FULL_SCOPE.knobs,
            warehouses=_selectors(warehouses),
        )
    LOGGER.info("config.scope scope=%s warehouses=%s", scope.describe(), len(scope.warehouses) or "all")
    return scope
