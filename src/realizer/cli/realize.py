"""``realizer``: one realize run, or one backtest, from command-line arguments.

The CLI only parses its own arguments, resolves the output layout (``realizer.config.output_layout``: a new run folder
under ``--output-dir`` for the projections and the log, the history in ``--state-dir``, default ``<output-dir>/state``;
``--flat-output`` writes both under ``--output-dir`` itself), loads the package, the observation and the policy (in that
order), configures logging, calls ``realizer.realizer.realize_package`` and prints the written package path (or the
snapshot with ``--print-json``). It reaches the observation sources (the demo's simulated world, a live measurement, an
evidence replay, an observation file) and the policy type through the facade and never imports a capability package.
The inputs are checked, and a demo or live measurement is written to the state, before the run folder is created, so a
refused input writes nothing; the log lines from the start of the run are written to the run folder's log once it exists.

``--publisher-run`` is a fleet run (``run_fleet``) instead of ``--package``: every warehouse of a Publisher run folder (or
the newest under a root), or those ``--warehouse`` names, from one source (``--profile`` with ``--sql-warehouse-id``,
``--demo`` or ``--from-evidence <folder>``), through the facade's ``realize_fleet``; it prints one line per warehouse and
the run manifest and workbook paths, and returns 1 when any warehouse crashed, else 2 when any was refused, else 0.

``--backtest`` is a separate mode with its own options (``parse_backtest_args``; ``realizer --backtest --help``): no
package, one source (``--profile`` with ``--sql-warehouse-id``, ``--from-evidence`` or ``--demo``), the facade's backtest
entry points, and a one-line summary per past change followed by the written report path.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from realizer.common.errors import InputError
from realizer.common.logging import LOGGER, attach_log_file, setup_logging
from realizer.config.loader import (SUPPORTED_PUBLISHER_CONTRACT, load_approvals, load_package, load_policy_mapping, load_publisher_run,
                                    select_warehouses)
from realizer.config.output_layout import RunLayout, resolve_layout, resolve_state_dir
from realizer.config.settings import PRODUCT_VERSION, REALIZER_VERSION
from realizer.contracts.fleet import SOURCE_DEMO, SOURCE_EVIDENCE, SOURCE_LIVE, FleetRequest, LiveSource
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION
from realizer.contracts.realization import CONTRACT_VERSION, LEDGER_CONTRACT_VERSION
from realizer.contracts.request import RealizationRequest
from realizer.realizer import (
    RealizerPolicy,
    backtest_demo,
    backtest_from_evidence,
    backtest_live,
    backtest_summary,
    build_context,
    load_observation,
    measure_demo,
    measure_live,
    observe_from_evidence,
    realize_fleet,
    realize_package,
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"Deterministic Databricks SQL Warehouse Realizer v{REALIZER_VERSION}",
                                epilog="Past auto-stop changes, no package: realizer --backtest --help")
    p.add_argument("--version", action="version", version=version_text())
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--package", help="Path to immutable RecommendationPackage JSON")
    target.add_argument("--publisher-run", help="Fleet run: a Publisher run folder, or a root holding run_date=*/run_ts=* "
                        "folders (the newest is used); every warehouse of its manifest, each open lineage kept in the state")
    p.add_argument("--observation", help="Path to normalized post-change observation JSON")
    p.add_argument("--output-dir", default="./warehouse_optimizer_out", help="Root of the run folders "
                   "(run_date=YYYY-MM-DD/run_ts=YYYYMMDDTHHMMSSZ/: realization packages, snapshots, realizer.log)")
    p.add_argument("--state-dir", help="Long-lived state: ledgers, evidence, regeneration requests, measurements, source "
                   "package copies, policies, state index (default <output-dir>/state)")
    p.add_argument("--flat-output", action="store_true", help="Write the state and the projections under --output-dir "
                   "itself (single-folder layout, no run folder, no log file)")
    p.add_argument("--policy", help="Optional Realizer policy override JSON")
    p.add_argument("--demo", action="store_true", help="Measure the package's Publisher demo-fleet warehouse in a simulated world "
                   "where its first eligible step was applied (demo-fleet packages only)")
    p.add_argument("--demo-modified", action="store_true", help="As --demo, with a size step to MEDIUM applied as SMALL")
    p.add_argument("--profile", help="Databricks CLI profile (OAuth U2M): measure the package's warehouse from system tables")
    p.add_argument("--sql-warehouse-id", help="Live: the SQL warehouse the read-only collector statements run on (never the measured one)")
    p.add_argument("--databricks-config-file", help="Live: Databricks CLI config file (default ~/.databrickscfg)")
    p.add_argument("--source-region", help="Live: AWS region of the modeled AWS cost (default: the package's source region)")
    p.add_argument("--aws-price-registry", help="Live: regional EC2 price registry JSON (default: the one committed with the Publisher)")
    p.add_argument("--as-of", help="Live or demo: measurement time, UTC ISO-8601 (default: now; demo: 28 settled days after the change)")
    p.add_argument("--billing-ingested-through", help="Live: only billing records ingested on or before this YYYY-MM-DD date")
    p.add_argument("--from-evidence", help="Replay the measurement evidence file a live or demo run wrote (realization/measurements/); "
                   "fleet run: a folder, each package's newest evidence file in it")
    p.add_argument("--warehouse", action="append", default=[], help="Fleet run: only this warehouse of the Publisher run "
                   "(<warehouse_id> or <workspace_id>/<warehouse_id>; repeatable; default every warehouse)")
    p.add_argument("--approvals", help="Live or replay (fleet run: any source): approvals file naming approved changes (their origin is then CONFIRMED)")
    p.add_argument("--replay-through", help="Project only ledger entries recorded through this UTC time")
    p.add_argument("--print-json", action="store_true")
    p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return p.parse_args(argv)


def version_text() -> str:
    """What ``--version`` prints: the Realizer, the release, and the contracts it reads and writes."""
    return (f"realizer {REALIZER_VERSION} (release {PRODUCT_VERSION}; reads Reporter JSON Contract {SUPPORTED_PUBLISHER_CONTRACT} "
            f"packages and observation format {OBSERVATION_FORMAT_VERSION}; writes Realizer Contract {CONTRACT_VERSION}, "
            f"ledger {LEDGER_CONTRACT_VERSION})")


def parse_backtest_args(argv: Sequence[str]) -> argparse.Namespace:
    """The options of ``realizer --backtest``: past auto-stop changes measured from system tables, no package."""
    p = argparse.ArgumentParser(prog="realizer --backtest", description="Backtest past auto-stop changes from system tables "
                                f"(Deterministic Databricks SQL Warehouse Realizer v{REALIZER_VERSION})")
    p.add_argument("--version", action="version", version=version_text())
    p.add_argument("--backtest", action="store_true", required=True, help="Backtest past auto-stop changes (no package)")
    p.add_argument("--package", help="Not used by a backtest (refused when given)")
    p.add_argument("--output-dir", default="./warehouse_optimizer_out")
    p.add_argument("--profile", help="Databricks CLI profile (OAuth U2M): read the past changes from system tables")
    p.add_argument("--sql-warehouse-id", help="Live: the SQL warehouse the read-only collector statements run on (never a backtested one)")
    p.add_argument("--warehouse", action="append", default=[], help="Live: backtest only this warehouse (<warehouse_id> or "
                   "<workspace_id>/<warehouse_id>; repeatable; default every warehouse)")
    p.add_argument("--databricks-config-file", help="Live: Databricks CLI config file (default ~/.databrickscfg)")
    p.add_argument("--source-region", help="Live: AWS region of the modeled AWS cost of Pro and Classic (without it: DBU only)")
    p.add_argument("--aws-price-registry", help="Live: regional EC2 price registry JSON (default: the one committed with the Publisher)")
    p.add_argument("--as-of", help="Live: backtest time, UTC ISO-8601 (default: now)")
    p.add_argument("--billing-ingested-through", help="Live: only billing records ingested on or before this YYYY-MM-DD date")
    p.add_argument("--from-evidence", help="Replay the backtest evidence file a backtest wrote (realization/backtest/evidence-*.json)")
    p.add_argument("--demo", action="store_true", help="Backtest a deterministic simulated fleet of past changes")
    p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return p.parse_args(argv)


def run_backtest(argv: Sequence[str]) -> int:
    """One backtest: refusals (InputError, one line naming options, never values) before anything is read or written."""
    args = parse_backtest_args(argv)
    setup_logging(args.log_level)
    LOGGER.info("realizer.backtest.start version=%s release=%s", REALIZER_VERSION, PRODUCT_VERSION)
    if args.package:
        raise InputError("--backtest reads no package: drop --package")
    sources = [name for name, given in (("--profile", args.profile), ("--from-evidence", args.from_evidence), ("--demo", args.demo)) if given]
    if len(sources) != 1:
        raise InputError("--backtest needs one source: --profile with --sql-warehouse-id, --from-evidence or --demo"
                         + (f", not {' and '.join(sources)}" if sources else ""))
    if args.warehouse and not args.profile:
        raise InputError("--warehouse selects warehouses of a live backtest (--profile) only")
    output_dir = Path(args.output_dir)
    if args.profile:
        report, path = backtest_live(output_dir, profile=args.profile, sql_warehouse_id=args.sql_warehouse_id, warehouses=args.warehouse,
                                     config_file=args.databricks_config_file, source_region=args.source_region,
                                     aws_price_registry=args.aws_price_registry, as_of=args.as_of, ingestion_cutoff=args.billing_ingested_through)
    elif args.from_evidence:
        report, path = backtest_from_evidence(args.from_evidence, output_dir)
    else:
        report, path = backtest_demo(output_dir)
    LOGGER.info("realizer.backtest.done changes=%d skipped=%d", len(report["changes"]), len(report["skipped"]))
    for line in backtest_summary(report):
        print(line)
    print(path)
    return 0


def run_layout(args: argparse.Namespace) -> RunLayout:
    """The layout of this run (``realizer.config.output_layout.resolve_layout``); the run folder is created and, outside
    the single-folder layout, the log is written to ``<run folder>/realizer.log`` from here on."""
    layout = resolve_layout(Path(args.output_dir), Path(args.state_dir) if args.state_dir else None, flat=args.flat_output)
    if layout.log_path is not None:
        attach_log_file(layout.log_path)
    LOGGER.info("realizer.layout run_dir=%s state_dir=%s flat=%s", layout.run_dir, layout.state_dir, layout.flat)
    return layout


def run(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--backtest" in argv:
        return run_backtest(argv)
    args = parse_args(argv)
    setup_logging(args.log_level)
    LOGGER.info("realizer.start version=%s release=%s", REALIZER_VERSION, PRODUCT_VERSION)
    if args.publisher_run:
        return run_fleet(args)
    if args.warehouse:
        raise InputError("--warehouse selects warehouses of a fleet run (--publisher-run)")
    sources = [name for name, given in (("--observation", args.observation), ("--demo", args.demo or args.demo_modified),
                                        ("--profile", args.profile), ("--from-evidence", args.from_evidence)) if given]
    if len(sources) > 1:
        raise InputError(f"choose one observation source, not {' and '.join(sources)}")
    if args.approvals and not (args.profile or args.from_evidence):
        raise InputError("--approvals applies to a measured change: use it with --profile or --from-evidence")
    package = load_package(args.package)
    approvals = load_approvals(args.approvals)
    observation: Optional[Mapping[str, Any]] = None
    if args.observation:
        observation = load_observation(args.observation)
    elif args.from_evidence:
        observation = observe_from_evidence(package, args.from_evidence, approvals)
    policy = RealizerPolicy.from_mapping(load_policy_mapping(args.policy))
    request = RealizationRequest(package=package, observation=observation, output_dir=Path(args.output_dir), policy=policy,
                                 replay_through=args.replay_through)
    build_context(request)  # a refused input writes nothing
    state_dir = resolve_state_dir(Path(args.output_dir), Path(args.state_dir) if args.state_dir else None, flat=args.flat_output)
    if args.demo or args.demo_modified:
        observation = measure_demo(package, state_dir, as_of=args.as_of, modified=args.demo_modified)
    elif args.profile:
        observation = measure_live(package, state_dir, profile=args.profile, sql_warehouse_id=args.sql_warehouse_id,
                                   config_file=args.databricks_config_file, source_region=args.source_region,
                                   aws_price_registry=args.aws_price_registry, as_of=args.as_of, ingestion_cutoff=args.billing_ingested_through,
                                   approvals=approvals)
    layout = run_layout(args)
    snapshot = realize_package(dataclasses.replace(request, observation=observation, output_dir=layout.run_dir, state_dir=layout.state_dir)).realization
    LOGGER.info("realizer.done rpkg=%s rsnap=%s state=%s", snapshot.realization_package_id, snapshot.snapshot_id,
                snapshot.current_state["realization_state"])
    if args.print_json:
        print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    else:
        print(layout.run_dir / "realization" / "packages" / f"{snapshot.realization_package_id}.json")
    return 0


# Options of a single-package run a fleet run refuses: each names one package's input or output.
PER_PACKAGE_OPTIONS = (("--observation", "observation"), ("--demo-modified", "demo_modified"), ("--replay-through", "replay_through"),
                       ("--print-json", "print_json"))


def fleet_request(args: argparse.Namespace) -> FleetRequest:
    """The fleet run the options ask for, checked before anything is written: one source (``--profile`` with
    ``--sql-warehouse-id``, ``--demo`` or ``--from-evidence <folder>``), no per-package option, the Publisher run and its
    selected warehouses, the approvals and the policy. The run folder and state directory are filled in later."""
    given = [flag for flag, name in PER_PACKAGE_OPTIONS if getattr(args, name)]
    if given:
        raise InputError(f"{' and '.join(given)} {'apply' if len(given) > 1 else 'applies'} to one package (--package), "
                         "not to a fleet run (--publisher-run)")
    sources = [name for name, flag in (("--profile", args.profile), ("--demo", args.demo), ("--from-evidence", args.from_evidence)) if flag]
    if len(sources) != 1:
        raise InputError("a fleet run needs one source: --profile with --sql-warehouse-id, --demo or --from-evidence <folder>"
                         + (f", not {' and '.join(sources)}" if sources else ""))
    if args.profile and not args.sql_warehouse_id:
        raise InputError("--profile needs --sql-warehouse-id (the SQL warehouse the collector statements run on)")
    run = select_warehouses(load_publisher_run(args.publisher_run), args.warehouse)
    evidence_dir = Path(args.from_evidence) if args.from_evidence else None
    if evidence_dir is not None and not evidence_dir.is_dir():
        raise InputError(f"a fleet run reads measurement evidence from a folder: {evidence_dir} is not one")
    approvals = load_approvals(args.approvals)
    policy = RealizerPolicy.from_mapping(load_policy_mapping(args.policy))
    live = None
    if args.profile:
        live = LiveSource(profile=args.profile, sql_warehouse_id=args.sql_warehouse_id, config_file=args.databricks_config_file,
                          source_region=args.source_region, aws_price_registry=args.aws_price_registry,
                          ingestion_cutoff=args.billing_ingested_through)
    source = SOURCE_LIVE if args.profile else SOURCE_DEMO if args.demo else SOURCE_EVIDENCE
    return FleetRequest(publisher_run=run, source=source, run_dir=Path(args.output_dir), state_dir=Path(args.output_dir), live=live,
                        evidence_dir=evidence_dir, as_of=args.as_of, approvals=approvals, policy=policy)


def run_fleet(args: argparse.Namespace) -> int:
    """``realizer --publisher-run``: every warehouse of the Publisher run; prints one line per warehouse, then the run
    manifest and workbook paths. Returns the fleet's exit status: 1 when any warehouse crashed, else 2 when any was
    refused, else 0. A refusal before any warehouse starts (an option, the Publisher run, the approvals or policy file)
    raises ``InputError`` and writes nothing."""
    request = fleet_request(args)
    layout = run_layout(args)
    report = realize_fleet(dataclasses.replace(request, run_dir=layout.run_dir, state_dir=layout.state_dir))
    for run in report.warehouse_runs:
        print(f"{run.workspace_id}/{run.warehouse_id}: {run.status}" + (f" ({run.reason})" if run.reason else ""))
    print(f"manifest: {report.manifest_path}")
    if report.workbook_path is not None:
        print(f"workbook: {report.workbook_path}")
    return report.exit_status
