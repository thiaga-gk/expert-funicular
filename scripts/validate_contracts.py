"""Validate the canonical reporter contract example against both JSON Schema and dataclass semantics."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402

from publisher.contracts.reporter_schema import build_schema  # noqa: E402
from examples.build_contract_example import build_package  # noqa: E402


def main() -> int:
    example = json.loads((ROOT / "examples/recommendation-package.example.json").read_text())
    errors = sorted(
        Draft202012Validator(build_schema(), format_checker=FormatChecker()).iter_errors(example),
        key=lambda e: list(e.path),
    )
    if errors:
        for error in errors:
            print(f"JSON Schema error at {list(error.path)}: {error.message}")
        return 1
    package = build_package()
    package.validate()
    if package.to_dict() != example:
        print("Canonical example differs from dataclass-generated example")
        return 1
    print(f"OK contract={package.contract_version} package_id={package.recommendation_package_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
