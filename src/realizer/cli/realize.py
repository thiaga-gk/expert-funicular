"""``realizer``: one realize run, or one backtest, from command-line arguments.

The CLI only parses its own arguments, loads the package, the observation and the policy (in that order), configures
logging, calls ``realizer.realizer.realize`` and prints the written package path (or the snapshot with ``--print-json``).
It reaches the demo observation, the observation loader and the policy type through the facade and never imports a
capability package.

``--backtest`` is a separate mode with its own options (``parse_backtest_args``; ``realizer --backtest --help``): no
package, one source (``--profile`` with ``--sql-warehouse-id``, ``--from-evidence`` or ``--demo``), the facade's backtest
entry points, and a one-line summary per past change followed by the written report path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from realizer.common.errors import InputError
from realizer.common.logging import LOGGER, setup_logging
from realizer.config.loader import SUPPORTED_PUBLISHER_CONTRACT, load_package, load_policy_mapping
from realizer.config.settings import PRODUCT_VERSION, REALIZER_VERSION
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION
from realizer.contracts.realization import CONTRACT_VERSION, LEDGER_CONTRACT_VERSION
from realizer.realizer import (
    RealizerPolicy,
    backtest_demo,
    backtest_from_evidence,
    backtest_live,
    backtest_summary,
    demo_observation,
    load_observation,
    measure_live,
    observe_from_evidence,
    realize,
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"Deterministic Databricks SQL Warehouse Realizer v{REALIZER_VERSION}")
    p.add_argument("--version", action="version", version=version_text())
    p.add_argument("--package", required=True, help="Path to immutable RecommendationPackage JSON")
    p.add_argument("--observation", help="Path to normalized post-change observation JSON")
    p.add_argument("--output-dir", default="./warehouse_optimizer_out")
    p.add_argument("--policy", help="Optional Realizer policy override JSON")
    p.add_argument("--demo", action="store_true", help="Use deterministic synthetic post-change observation")
    p.add_argument("--demo-modified", action="store_true", help="Demo a modified size target when possible")
    p.add_argument("--profile", help="Databricks CLI profile (OAuth U2M): measure the package's warehouse from system tables")
    p.add_argument("--sql-warehouse-id", help="Live: the SQL warehouse the read-only collector statements run on (never the measured one)")
    p.add_argument("--databricks-config-file", help="Live: Databricks CLI config file (default ~/.databrickscfg)")
    p.add_argument("--source-region", help="Live: AWS region of the modeled AWS cost (default: the package's source region)")
    p.add_argument("--aws-price-registry", help="Live: regional EC2 price registry JSON (default: the one committed with the Publisher)")
    p.add_argument("--as-of", help="Live: measurement time, UTC ISO-8601 (default: now)")
    p.add_argument("--billing-ingested-through", help="Live: only billing records ingested on or before this YYYY-MM-DD date")
    p.add_argument("--from-evidence", help="Replay the measurement evidence file a live run wrote (realization/measurements/)")
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


def run(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--backtest" in argv:
        return run_backtest(argv)
    args = parse_args(argv)
    setup_logging(args.log_level)
    LOGGER.info("realizer.start version=%s release=%s", REALIZER_VERSION, PRODUCT_VERSION)
    sources = [name for name, given in (("--observation", args.observation), ("--demo", args.demo or args.demo_modified),
                                        ("--profile", args.profile), ("--from-evidence", args.from_evidence)) if given]
    if len(sources) > 1:
        raise InputError(f"choose one observation source, not {' and '.join(sources)}")
    package = load_package(args.package)
    observation: Optional[Mapping[str, Any]] = None
    if args.demo or args.demo_modified:
        observation = demo_observation(package, modified=args.demo_modified)
    elif args.observation:
        observation = load_observation(args.observation)
    elif args.profile:
        observation = measure_live(package, Path(args.output_dir), profile=args.profile, sql_warehouse_id=args.sql_warehouse_id,
                                   config_file=args.databricks_config_file, source_region=args.source_region,
                                   aws_price_registry=args.aws_price_registry, as_of=args.as_of, ingestion_cutoff=args.billing_ingested_through)
    elif args.from_evidence:
        observation = observe_from_evidence(package, args.from_evidence)
    policy = RealizerPolicy.from_mapping(load_policy_mapping(args.policy))
    snapshot = realize(package, observation, Path(args.output_dir), policy=policy, replay_through=args.replay_through)
    LOGGER.info("realizer.done rpkg=%s rsnap=%s state=%s", snapshot.realization_package_id, snapshot.snapshot_id,
                snapshot.current_state["realization_state"])
    if args.print_json:
        print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    else:
        print(Path(args.output_dir) / "realization" / "packages" / f"{snapshot.realization_package_id}.json")
    return 0
