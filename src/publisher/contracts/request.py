"""Run request and context: what one publisher run was asked to do, and the policy it runs under."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from publisher.governor.thresholds import Thresholds

MODE_DEMO = "demo"
MODE_REPLAY = "replay"
MODE_LIVE = "live"


@dataclass(frozen=True)
class PublisherRequest:
    """The CLI request for one run. ``mode`` selects the evidence source: the demo simulation, replay files or live."""
    mode: str
    output_dir: Path
    demo_count: int = 0
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


@dataclass(frozen=True)
class RunContext:
    """What every capability receives: the request and the policy thresholds of this run."""
    request: PublisherRequest
    thresholds: Thresholds
