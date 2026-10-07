"""``realizer``: one realize run from command-line arguments.

The CLI only parses its own arguments, loads the package, the observation and the policy (in that order), configures
logging, calls ``realizer.realizer.realize`` and prints the written package path (or the snapshot with ``--print-json``).
It reaches the demo observation, the observation loader and the policy type through the facade and never imports a
capability package.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from realizer.common.errors import InputError
from realizer.common.logging import LOGGER, setup_logging
from realizer.config.loader import SUPPORTED_PUBLISHER_CONTRACT, load_package, load_policy_mapping
from realizer.config.settings import PRODUCT_VERSION, REALIZER_VERSION
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION
from realizer.contracts.realization import CONTRACT_VERSION, LEDGER_CONTRACT_VERSION
from realizer.realizer import RealizerPolicy, demo_observation, load_observation, measure_live, observe_from_evidence, realize


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


def run(argv: Optional[Sequence[str]] = None) -> int:
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
