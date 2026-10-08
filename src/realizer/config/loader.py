"""Input loading: the source package and the check that it is a package this Realizer reads, and the optional policy
override file.

The Realizer reads exactly the Reporter JSON Contract the Publisher writes (``SUPPORTED_PUBLISHER_CONTRACT``). A package
is accepted only when it passes the Publisher's committed JSON Schema and its id and full hash match its content, so a
package edited after publishing, or written by another contract version, never reaches the engine.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from publisher.config.defaults import SUPPORTED_WAREHOUSE_TYPES
from publisher.contracts import reporter_contract as publisher_contract
from publisher.reviewer.package import validate_locked_package_schema
import json

from realizer.common.errors import InputError
from realizer.common.utils import load_json, parse_utc, utc_iso
from realizer.config.output_layout import PUBLISHER_MANIFEST_NAME, is_realizer_run_dir, latest_publisher_run_dir
from realizer.contracts.approvals import APPROVALS_FORMAT_VERSION, Approvals, approvals_schema
from realizer.contracts.measurement import MEASUREMENT_EVIDENCE_FORMAT_VERSION, measurement_evidence_schema
from realizer.contracts.fleet import PublisherRun, PublisherRunEntry
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION, observation_schema

SUPPORTED_PUBLISHER_CONTRACT = "3.2.0"


def read_json_input(path: str | Path, label: str) -> Any:
    """The JSON document at *path*; an unreadable file raises ``InputError`` naming the *label*, the path and why."""
    try:
        return load_json(path)
    except FileNotFoundError:
        raise InputError(f"{label} file not found: {path}") from None
    except IsADirectoryError:
        raise InputError(f"{label} path is a directory, not a JSON file: {path}") from None
    except json.JSONDecodeError as exc:
        raise InputError(f"{label} file is not valid JSON: {path} (line {exc.lineno})") from None
    except UnicodeDecodeError:
        raise InputError(f"{label} file is not UTF-8 text: {path}") from None


def load_package(path: str | Path) -> Any:
    """The RecommendationPackage JSON at *path*, parsed (it is checked by ``validate_package``)."""
    return read_json_input(path, "package")


def load_policy_mapping(path: Optional[str | Path]) -> Optional[Any]:
    """The policy override mapping at *path*, or ``None`` when no file is given (``RealizerPolicy.from_mapping`` input)."""
    return read_json_input(path, "policy") if path else None


def validate_replay_through(value: Optional[str], package: Mapping[str, Any]) -> Optional[str]:
    """The replay cut as a UTC time (``None`` when no cut is asked for). A cut that is not a time, or that is before the
    package was generated (when the lineage starts, so the cut would leave nothing to project), raises ``InputError``."""
    if value is None:
        return None
    try:
        cut = utc_iso(value)
    except (TypeError, ValueError):
        raise InputError("replay cut (--replay-through) is not an ISO-8601 time") from None
    generated = (package.get("provenance") or {}).get("generated_at")
    if generated and parse_utc(cut) < parse_utc(generated):
        raise InputError(f"replay cut {cut} is before the package was generated ({generated}): nothing to project")
    return cut


def validate_package(package: Any) -> None:
    """Accept *package* only if it is an unmodified Publisher package of the supported contract; raise ``ValueError``.

    Checks, in order: a mapping; ``contract_version`` equal to ``SUPPORTED_PUBLISHER_CONTRACT``; the Publisher's committed
    Draft 2020-12 schema; the ``PKG-`` id and ``provenance.package_hash_full_sha256`` recomputed from the hashed fields;
    the ``realization`` block is the Publisher's empty placeholder (status NOT_APPLIED, no records; the Realizer's own
    lineage is the RPKG, never this block); no carried-forward plan (reserved, never published yet); the recommended
    scenario exists; a supported warehouse type; and no plan step recommending its current value (matching would read it
    as applied whatever the warehouse runs; the Publisher never writes one).
    """
    if not isinstance(package, Mapping):
        raise InputError("a RecommendationPackage must be a JSON object")
    version = package.get("contract_version")
    if version != SUPPORTED_PUBLISHER_CONTRACT:
        raise InputError(f"unsupported Publisher contract: {version} (this Realizer reads {SUPPORTED_PUBLISHER_CONTRACT})")
    try:
        validate_locked_package_schema(package)
    except Exception as exc:  # jsonschema.ValidationError: report the location, never the package values
        location = "/".join(str(p) for p in getattr(exc, "absolute_path", ())) or "<root>"
        raise InputError(f"package fails the Reporter JSON Contract {SUPPORTED_PUBLISHER_CONTRACT} schema at {location}") from exc
    payload = publisher_contract.package_hash_payload_from_document(package)
    if publisher_contract.content_id("PKG", payload) != package["recommendation_package_id"]:
        raise InputError(f"package id {package['recommendation_package_id']} does not match the package content")
    if publisher_contract.sha256_hex(payload) != package["provenance"]["package_hash_full_sha256"]:
        raise InputError("provenance.package_hash_full_sha256 does not match the package content")
    realization = package["realization"]
    if realization.get("status") != "NOT_APPLIED" or realization.get("realization_records"):
        raise InputError("package realization block must be the Publisher's empty NOT_APPLIED placeholder")
    if package["carried_forward_plan"] is not None:
        raise InputError("packages with a carried-forward plan are not supported yet")
    recommended = package["recommended_plan"].get("scenario_id")
    if recommended is not None and recommended not in {s["scenario_id"] for s in package["scenarios"]}:
        raise InputError(f"recommended_plan.scenario_id {recommended} names no scenario of the package")
    warehouse_type = str(package["current_config"].get("warehouse_type") or "").upper()
    if warehouse_type not in SUPPORTED_WAREHOUSE_TYPES:
        raise InputError(f"unsupported warehouse type in the package: {warehouse_type or None}")
    for scenario in package["scenarios"]:
        for step in scenario["execution_plan"]:
            if step.get("step_type") == "CANDIDATE" and step.get("recommended_value") == step.get("current_value"):
                raise InputError(f"plan step {step.get('step')} of scenario {scenario['scenario_id']} recommends its current value")


TIME_FIELDS = ("observed_at_utc", "recorded_at_utc", "applied_at_utc", "window_start_utc", "window_end_utc")


def _time(observation: Mapping[str, Any], field: str) -> Optional[dt.datetime]:
    """The observation's *field* as an aware datetime (``None`` when null); a value that is not an ISO-8601 time with a
    UTC offset raises ``ValueError`` naming the field, never the value."""
    value = observation[field]
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise InputError(f"observation {field} is not an ISO-8601 time with a UTC offset")
    return parsed


def validate_observation(observation: Any, package: Mapping[str, Any]) -> None:
    """Accept *observation* only if it is a well-formed observation of *package*'s warehouse; raise ``ValueError``.

    Checks, in order: observation format ``OBSERVATION_FORMAT_VERSION`` against its JSON Schema (every key present,
    strict types, enumerated vocabularies); the observation names the package's workspace and warehouse; its times are
    ISO-8601 with a UTC offset and ordered (applied at or before observed, window start at or before window end, window end at or before observed);
    measured economics come with a window; their cost components agree with their costs (``_check_components``);
    ``applied_scenario_id`` names a scenario of the package; a ``replacement_package_id`` (a package id in the contract
    format, checked by the schema) is not the package itself; the observed warehouse type is a supported one. The
    messages name the failing field, never its value, except ids and the type.
    """
    import jsonschema  # a gate that cannot run must not pass; jsonschema is a declared dependency

    if not isinstance(observation, Mapping):
        raise InputError("an observation must be a JSON object")
    if observation.get("observation_format_version") != OBSERVATION_FORMAT_VERSION:
        raise InputError(f"unsupported observation format: {observation.get('observation_format_version')} (this Realizer reads {OBSERVATION_FORMAT_VERSION})")
    validator = jsonschema.Draft202012Validator(observation_schema(), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    error = next(iter(sorted(validator.iter_errors(dict(observation)), key=lambda e: list(e.absolute_path))), None)
    if error is not None:
        location = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise InputError(f"observation fails format {OBSERVATION_FORMAT_VERSION} at {location} ({error.validator})")
    warehouse = package["warehouse"]
    if (observation["workspace_id"], observation["warehouse_id"]) != (warehouse["workspace_id"], warehouse["warehouse_id"]):
        raise InputError(f"observation is for {observation['workspace_id']}/{observation['warehouse_id']}, "
                         f"the package for {warehouse['workspace_id']}/{warehouse['warehouse_id']}")
    times = {field: _time(observation, field) for field in TIME_FIELDS}
    observed, applied = times["observed_at_utc"], times["applied_at_utc"]
    start, end = times["window_start_utc"], times["window_end_utc"]
    if applied is not None and applied > observed:
        raise InputError("observation applied_at_utc is after observed_at_utc")
    if start is not None and end is not None and start > end:
        raise InputError("observation window_start_utc is after window_end_utc")
    if end is not None and end > observed:
        raise InputError("observation window_end_utc is after observed_at_utc")
    if observation["economics"] is not None and (start is None or end is None):
        raise InputError("observation economics need window_start_utc and window_end_utc")
    if observation["economics"] is not None:
        _check_components(observation["economics"])
    scenario = observation["applied_scenario_id"]
    if scenario is not None and scenario not in {s["scenario_id"] for s in package["scenarios"]}:
        raise InputError(f"applied_scenario_id {scenario} names no scenario of the package")
    if observation["replacement_package_id"] == package["recommendation_package_id"]:
        raise InputError(f"replacement_package_id {observation['replacement_package_id']} is the package itself: a package cannot replace itself")
    observed_type = observation["config"]["warehouse_type"].strip().upper()
    if observed_type not in SUPPORTED_WAREHOUSE_TYPES:
        raise InputError(f"unsupported warehouse type observed: {observed_type}")


# How far the cost components may differ from the realized total (the Reporter JSON Contract's reconciliation
# tolerance: amounts rounded to cents, added).
RECONCILIATION_TOLERANCE = Decimal("0.03")
COMPONENT_FIELDS = ("dbu_realized", "aws_economic_realized", "aws_cash_realized", "aws_commitment_freed")


def _amount(economics: Mapping[str, Any], field: str) -> Optional[Decimal]:
    """The economics' *field* as a finite ``Decimal`` (``None`` when null); a value that is not a finite number raises
    ``InputError`` naming the field, never the value."""
    value = economics[field]
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        amount = None
    if amount is None or not amount.is_finite():
        raise InputError(f"observation economics {field} is not a finite number")
    return amount


def _check_components(economics: Mapping[str, Any]) -> None:
    """Accept the cost components of *economics* only if they agree with its costs; raise ``InputError``.

    An AWS component needs a DBU component and an evidence type; an AWS cash or freed commitment amount needs
    ``CUR_ACTUAL`` evidence (modeled AWS is never cash). With both costs stated, the realized total is
    ``counterfactual_old_cost - observed_new_cost``: ``dbu_realized`` alone is that total, and ``dbu_realized +
    aws_economic_realized`` is that total, each within ``RECONCILIATION_TOLERANCE``; without both costs the economics
    are unavailable and the total is not checked.
    """
    old, new = _amount(economics, "counterfactual_old_cost"), _amount(economics, "observed_new_cost")
    dbu, aws, cash, freed = (_amount(economics, field) for field in COMPONENT_FIELDS)
    evidence_type = economics["aws_evidence_type"]
    if aws is not None and dbu is None:
        raise InputError("observation economics: AWS component without a DBU component (aws_economic_realized needs dbu_realized)")
    if aws is not None and evidence_type is None:
        raise InputError("observation economics: AWS component without an evidence type (aws_economic_realized needs aws_evidence_type)")
    for field, amount in (("aws_cash_realized", cash), ("aws_commitment_freed", freed)):
        if amount is not None and evidence_type != "CUR_ACTUAL":
            raise InputError(f"observation economics {field} needs aws_evidence_type CUR_ACTUAL (modeled AWS is never cash)")
    if dbu is not None and old is not None and new is not None:
        given = dbu + (aws or Decimal("0"))
        if abs(given - (old - new)) > RECONCILIATION_TOLERANCE:
            fields = "dbu_realized + aws_economic_realized" if aws is not None else "dbu_realized"
            raise InputError(f"observation economics {fields} does not equal counterfactual_old_cost - observed_new_cost "
                             f"(tolerance {RECONCILIATION_TOLERANCE})")


def validate_measurement_evidence(evidence: Any) -> None:
    """Accept *evidence* only in measurement evidence format ``MEASUREMENT_EVIDENCE_FORMAT_VERSION`` (its JSON Schema);
    raise ``InputError`` naming the failing location, never a value."""
    import jsonschema  # a gate that cannot run must not pass; jsonschema is a declared dependency

    if not isinstance(evidence, Mapping):
        raise InputError("measurement evidence must be a JSON object")
    version = evidence.get("measurement_evidence_format_version")
    if version != MEASUREMENT_EVIDENCE_FORMAT_VERSION:
        raise InputError(f"unsupported measurement evidence format: {version} (this Realizer reads {MEASUREMENT_EVIDENCE_FORMAT_VERSION})")
    validator = jsonschema.Draft202012Validator(measurement_evidence_schema(), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    error = next(iter(sorted(validator.iter_errors(dict(evidence)), key=lambda e: list(e.absolute_path))), None)
    if error is not None:
        location = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise InputError(f"measurement evidence fails format {MEASUREMENT_EVIDENCE_FORMAT_VERSION} at {location} ({error.validator})")


def load_approvals(path: Optional[str | Path]) -> Optional[Any]:
    """The approvals file at *path* (format ``APPROVALS_FORMAT_VERSION``), checked against its schema, as an ``Approvals``
    mapping that records which approvals the run's measurements matched; ``None`` when no file is given. A file in another format is refused (``InputError``), naming the location, never a value."""
    if not path:
        return None
    import jsonschema

    approvals = read_json_input(path, "approvals")
    if not isinstance(approvals, Mapping) or approvals.get("approvals_format_version") != APPROVALS_FORMAT_VERSION:
        version = approvals.get("approvals_format_version") if isinstance(approvals, Mapping) else None
        raise InputError(f"unsupported approvals format: {version} (this Realizer reads {APPROVALS_FORMAT_VERSION})")
    error = next(iter(sorted(jsonschema.Draft202012Validator(approvals_schema()).iter_errors(dict(approvals)), key=lambda e: list(e.absolute_path))), None)
    if error is not None:
        location = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise InputError(f"approvals file fails format {APPROVALS_FORMAT_VERSION} at {location} ({error.validator})")
    return Approvals(approvals)


def load_publisher_run(path: str | Path) -> PublisherRun:
    """The Publisher run at *path*: a run folder (it holds ``manifest.json``), or a root holding
    ``run_date=*/run_ts=*`` run folders, of which the newest Publisher run is read (``latest_publisher_run_dir``: the Realizer's
    own run folders under a shared root are skipped). Its manifest's ``packages``
    entries (``path``, ``workspace_id``, ``warehouse_id``, ``recommendation_package_id``) become the run's entries in
    (workspace, warehouse) order; the packages themselves are read per warehouse by the fleet run. No run, an unreadable
    manifest, an entry missing a field, a path outside the run folder or two packages of one warehouse raise
    ``InputError``."""
    try:
        run_dir = latest_publisher_run_dir(Path(path))
    except (FileNotFoundError, NotADirectoryError):
        raise InputError(f"no Publisher run (manifest.json or run_date=*/run_ts=* folders) under {path}") from None
    if is_realizer_run_dir(run_dir):
        raise InputError(f"{run_dir} is a Realizer run, not a Publisher run")
    manifest = read_json_input(run_dir / PUBLISHER_MANIFEST_NAME, "Publisher run manifest")
    packages = manifest.get("packages") if isinstance(manifest, Mapping) else None
    if not isinstance(packages, list):
        raise InputError(f"Publisher run manifest {run_dir / PUBLISHER_MANIFEST_NAME} has no packages list")
    entries = []
    for index, item in enumerate(packages):
        fields = ("path", "workspace_id", "warehouse_id", "recommendation_package_id")
        if not isinstance(item, Mapping) or not all(isinstance(item.get(f), str) and item.get(f) for f in fields):
            raise InputError(f"Publisher run manifest entry {index} needs {', '.join(fields)}")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise InputError(f"Publisher run manifest entry {index} names a package outside the run folder")
        entries.append(PublisherRunEntry(workspace_id=item["workspace_id"], warehouse_id=item["warehouse_id"],
                                         recommendation_package_id=item["recommendation_package_id"], path=relative.as_posix()))
    entries.sort(key=lambda e: (e.workspace_id, e.warehouse_id))
    keys = [(e.workspace_id, e.warehouse_id) for e in entries]
    if len(set(keys)) != len(keys):
        raise InputError("Publisher run manifest lists two packages of one warehouse")
    count = manifest.get("package_count")
    return PublisherRun(run_dir=run_dir, package_count=count if isinstance(count, int) and not isinstance(count, bool) else len(entries),
                        entries=tuple(entries))


def select_warehouses(run: PublisherRun, warehouses: Sequence[str]) -> PublisherRun:
    """*run* with only the entries *warehouses* names (``<warehouse_id>`` or ``<workspace_id>/<warehouse_id>``; none:
    every entry). A name that matches no entry raises ``InputError`` before anything runs."""
    if not warehouses:
        return run
    chosen = set()
    for name in warehouses:
        workspace, _, warehouse = name.rpartition("/")
        matches = [e for e in run.entries if e.warehouse_id == warehouse and (not workspace or e.workspace_id == workspace)]
        if not matches:
            raise InputError(f"--warehouse {name} is not a warehouse of the Publisher run")
        chosen.update((e.workspace_id, e.warehouse_id) for e in matches)
    return dataclasses.replace(run, entries=tuple(e for e in run.entries if (e.workspace_id, e.warehouse_id) in chosen))
