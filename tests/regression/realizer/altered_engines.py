"""Deliberately wrong candidate engines for ``test_compare_script``: they prove that
``scripts/compare_realizer_single_file.py`` fails when a candidate differs from the oracle.

They are importable by name (``--candidate tests.regression.realizer.altered_engines:drifting``) and wrap the modular
engine (``src/realizer``), so the only difference from a faithful candidate is the deliberate fault.
"""
from __future__ import annotations

from pathlib import Path

from tests.regression.realizer import canonical


def drifting() -> canonical.Engine:
    """The modular engine whose ``realize`` leaves one extra blank line at the end of every ledger file it wrote."""
    base = canonical.modular_engine()

    def realize(package, observation, out, **kwargs):
        snapshot = base.realize(package, observation, out, **kwargs)
        for ledger in sorted((Path(out) / "realization" / "ledger").glob("*.jsonl")):
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write("\n")
        return snapshot

    return canonical.Engine("drifting modular engine", realize, base.policy, base.compatibility, base.report)


def crashing() -> canonical.Engine:
    """The modular engine whose ``realize`` always raises."""
    base = canonical.modular_engine()

    def realize(package, observation, out, **kwargs):
        raise RuntimeError("deliberate candidate failure")

    return canonical.Engine("crashing modular engine", realize, base.policy, base.compatibility, base.report)


def not_an_engine() -> str:
    """A factory that returns something that is not an engine (resolution error)."""
    return "nope"
