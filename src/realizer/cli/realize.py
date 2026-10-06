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

from realizer.common.logging import setup_logging
from realizer.config.loader import load_package, load_policy_mapping
from realizer.realizer import RealizerPolicy, demo_observation, load_observation, realize


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deterministic Databricks SQL Warehouse Realizer v1.0.0")
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


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    package = load_package(args.package)
    observation: Optional[Mapping[str, Any]] = None
    if args.demo or args.demo_modified:
        observation = demo_observation(package, modified=args.demo_modified)
    elif args.observation:
        observation = load_observation(args.observation)
    policy = RealizerPolicy.from_mapping(load_policy_mapping(args.policy))
    snapshot = realize(package, observation, Path(args.output_dir), policy=policy, replay_through=args.replay_through)
    if args.print_json:
        print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    else:
        print(Path(args.output_dir) / "realization" / "packages" / f"{snapshot.realization_package_id}.json")
    return 0
