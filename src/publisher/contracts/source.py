"""Normalized source evidence: configuration snapshots, epochs and the per-warehouse evidence contract."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class WarehouseConfig:
    """Canonical SQL warehouse configuration snapshot used to form config epochs."""
    warehouse_type: str
    warehouse_channel: str
    warehouse_size: str
    min_clusters: int
    max_clusters: int
    auto_stop_minutes: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PhotonObservation:
    """Billing-derived Photon observation for the current warehouse epoch.

    This is an observed workload state, not a direct warehouse configuration read.
    It is derived from signed DBU usage attributed to product_features.is_photon.
    """
    status: str  # ENABLED | DISABLED | MIXED | UNKNOWN
    enabled_usage_pct: Optional[float]
    known_feature_coverage_pct: float
    true_dbu: float
    false_dbu: float
    null_dbu: float
    latest_usage_time: Optional[str]
    freshness_days: Optional[int]
    confidence: Dict[str, Any]
    evidence_source: str = "system.billing.usage.product_features.is_photon"


@dataclass(frozen=True)
class QuerySourceProfile:
    """Multi-label query-source profile for a warehouse's current epoch."""
    categories: Dict[str, int]
    percentages: Dict[str, float]
    primary_category: str
    client_applications: List[str]
    multi_label: bool = True


@dataclass(frozen=True)
class WarehouseAttribution:
    """Human/business attribution inferred from billing and query history."""
    owner: Optional[str]
    observed_owners: List[str]
    owner_source: str
    query_source: QuerySourceProfile


@dataclass
class EpochEvidence:
    """Normalized evidence attached to one immutable warehouse configuration epoch."""
    epoch_id: str
    warehouse_id: str
    workspace_id: str
    account_id: str
    warehouse_name: str
    epoch_start: str
    epoch_end: Optional[str]
    is_current: bool
    config: WarehouseConfig
    tags: Dict[str, str] = field(default_factory=dict)
    query: Dict[str, Any] = field(default_factory=dict)
    events: Dict[str, Any] = field(default_factory=dict)
    gaps: Dict[str, Any] = field(default_factory=dict)
    cost: Dict[str, Any] = field(default_factory=dict)
    # Daily resource series (current epoch only): None = not collected (older evidence), [] = collected, no activity.
    resource_daily: Optional[List[Dict[str, Any]]] = None


@dataclass
class WarehouseEvidence:
    """Complete normalized evidence contract for one workspace/warehouse identity."""
    warehouse_id: str
    workspace_id: str
    account_id: str
    warehouse_name: str
    source_region: str
    analysis_time: str
    current_epoch_id: str
    epochs: List[EpochEvidence]
    current_cost_365: Optional[float]  # None = unknown (unpriced usage in the trailing window); never 0.0
    baseline_forecast_cost_365: Optional[float]  # None = unknown forward DBU cost; never 0.0
    currency: str
    cost_history_days: int
    cost_cv_84: Optional[float]  # None = unknown (history too short or no cost basis); never 0.0
    forecast_metadata: Dict[str, Any] = field(default_factory=dict)
    daily_cost_365: List[Dict[str, Any]] = field(default_factory=list)
    billing_features: Dict[str, Any] = field(default_factory=dict)
    aws_baseline_cost_365: Optional[float] = None
    aws_cost_model: Dict[str, Any] = field(default_factory=dict)
    sql_sku_prices: Dict[str, Any] = field(default_factory=dict)
    compatibility_evidence: Dict[str, Any] = field(default_factory=dict)
    photon_observation: Optional[PhotonObservation] = None
    attribution: Optional[WarehouseAttribution] = None

    @property
    def dbu_365d_baseline_cost(self) -> Optional[float]:
        return self.baseline_forecast_cost_365

    @property
    def total_365d_baseline_cost(self) -> Optional[float]:
        if self.aws_baseline_cost_365 is None or self.baseline_forecast_cost_365 is None:
            return None
        return round(self.baseline_forecast_cost_365 + self.aws_baseline_cost_365, 2)

    @property
    def current_epoch(self) -> EpochEvidence:
        for e in self.epochs:
            if e.epoch_id == self.current_epoch_id:
                return e
        raise ValueError(f"Current epoch missing for {self.warehouse_id}: {self.current_epoch_id}")
