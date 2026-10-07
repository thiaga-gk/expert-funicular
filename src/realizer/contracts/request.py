"""Run request: what one Realizer run is asked to do.

The engine's entry point ``realizer.realizer.realize(package, observation, output_dir, *, policy=None,
replay_through=None)`` takes these values as arguments today; ``RealizationRequest`` names the same inputs as one
frozen value for the capability pipeline the reorganization builds toward (docs/architecture/realizer/repo-reorg-design.md 4.4). It adds
no input, default or validation of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class RealizationRequest:
    """The inputs of one realize run.

    ``package`` is the immutable RecommendationPackage (PKG-*) primitive; the engine never mutates it.
    ``observation`` is the normalized post-change observation; ``None`` or an empty mapping means a lineage-only run.
    ``output_dir`` is the directory the run's projections are written under (``realization/packages/`` and
    ``realization/snapshots/``).
    ``policy`` is the Realizer policy (``RealizerPolicy``); ``None`` means its defaults.
    ``replay_through`` projects only ledger entries recorded through this UTC time.
    ``state_dir`` is the directory the warehouse's history is kept under (``realization/`` ledgers, evidence,
    regeneration requests, measurements, source package copies, policies and state index); ``None`` means
    ``output_dir`` (the single-folder layout).
    """
    package: Mapping[str, Any]
    observation: Optional[Mapping[str, Any]]
    output_dir: Path
    policy: Optional[Any] = None
    replay_through: Optional[str] = None
    state_dir: Optional[Path] = None

    @property
    def state_root(self) -> Path:
        """Where the history is kept: ``state_dir``, else ``output_dir``."""
        return self.output_dir if self.state_dir is None else self.state_dir


@dataclass(frozen=True)
class RunContext:
    """A validated request: the source package passed the shape gate, the policy default is applied and the realization
    package id (RPKG-*) is minted. Every capability's ``run`` takes it."""
    request: RealizationRequest
    policy: Any
    realization_package_id: str
