"""Run request and context: what one publisher run was asked to do, and the policy it runs under."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from publisher.contracts.scope import FULL_SCOPE, RunScope
from publisher.governor.thresholds import Thresholds

MODE_DEMO = "demo"
MODE_REPLAY = "replay"
MODE_LIVE = "live"


@dataclass(frozen=True)
class PublisherRequest:
    """The CLI request for one run. ``mode`` selects the evidence source: the demo simulation, replay files or live.

    ``scope_specs`` (``scenario[:technique,...]``), ``scope_file`` and ``warehouses`` (``all`` or warehouse IDs) are raw
    run-scope inputs; the config layer resolves them into ``RunContext.scope``. Empty means the full scope.
    """
    mode: str
    output_dir: Path
    demo_count: int = 0
    # --demo-as-of (UTC ISO-8601); None = the demo's fixed analysis time. Resolved by the analyzer.
    demo_analysis_time: Optional[str] = None
    evidence_files: Tuple[str, ...] = ()
    profile: str = "DEFAULT"
    execution_kind: Optional[str] = None
    execution_id: Optional[str] = None
    config_file: Optional[str] = None
    workspace_id: Optional[str] = None
    source_region: str = "unknown"
    aws_price_registry: Optional[str] = None
    compatibility_api_mode: str = "auto"
    billing_ingested_through: Optional[str] = None
    allow_missing_aws_price: bool = False
    batch_size: int = 100
    generate_workbook: bool = True
    print_json: bool = False
    execution_mode: str = "local"
    # Raw --cost-profile flags (PROFILE or PROFILE=WH[,WH...]); parsed by config/cost_profiles.py.
    cost_profiles: Tuple[str, ...] = ()
    scope_specs: Tuple[str, ...] = ()
    scope_file: Optional[str] = None
    warehouses: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RunContext:
    """What every capability receives: the request, the policy thresholds and the resolved run scope."""
    request: PublisherRequest
    thresholds: Thresholds
    scope: RunScope = FULL_SCOPE
