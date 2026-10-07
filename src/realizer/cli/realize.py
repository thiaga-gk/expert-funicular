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

from realizer.common.logging import LOGGER, setup_logging
from realizer.config.loader import SUPPORTED_PUBLISHER_CONTRACT, load_package, load_policy_mapping
from realizer.config.settings import PRODUCT_VERSION, REALIZER_VERSION
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION
from realizer.contracts.realization import CONTRACT_VERSION, LEDGER_CONTRACT_VERSION
from realizer.realizer import RealizerPolicy, demo_observation, load_observation, realize


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=f"Deterministic Databricks SQL Warehouse Realizer v{REALIZER_VERSION}")
    p.add_argument("--version", action="version", version=version_text())
    p.add_argument("--package", required=True, help="Path to immutable RecommendationPackage JSON")
    p.add_argument("--observation", help="Path to normalized post-change observation JSON")
    p.add_argument("--output-dir", default="./warehouse_optimizer_out")
    p.add_argument("--policy", help="Optional Realizer policy override JSON")
    p.add_argument("--demo", action="store_true", help="Use deterministic synthetic post-change observation")
    p.add_argument("--demo-modified", action="store_true", help="Demo a modified size target when possible")
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
    package = load_package(args.package)
    observation: Optional[Mapping[str, Any]] = None
    if args.demo or args.demo_modified:
        observation = demo_observation(package, modified=args.demo_modified)
    elif args.observation:
        observation = load_observation(args.observation)
    policy = RealizerPolicy.from_mapping(load_policy_mapping(args.policy))
    snapshot = realize(package, observation, Path(args.output_dir), policy=policy, replay_through=args.replay_through)
    LOGGER.info("realizer.done rpkg=%s rsnap=%s state=%s", snapshot.realization_package_id, snapshot.snapshot_id,
                snapshot.current_state["realization_state"])
    if args.print_json:
        print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    else:
        print(Path(args.output_dir) / "realization" / "packages" / f"{snapshot.realization_package_id}.json")
    return 0
