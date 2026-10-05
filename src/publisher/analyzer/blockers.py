"""Evidence-level blockers and the withheld-baseline rationale."""
from __future__ import annotations

from typing import List, Optional

from publisher.contracts.source import WarehouseEvidence
from publisher.config.thresholds import Thresholds


def failure_blockers(failure_pct: Optional[float], t: "Thresholds") -> List[str]:
    """Reliability guard for every capacity cut: a high or unknown failed/cancelled share holds the cut."""
    if failure_pct is None:
        return ["FAILURE_CANCEL_EVIDENCE_UNAVAILABLE"]
    return ["FAILURE_CANCEL_RATE_HIGH"] if failure_pct > t.failure_cancel_pct_downsize_safe else []


def dbu_baseline_blocker(e: "WarehouseEvidence", t: "Thresholds") -> str:
    """Why the forward DBU baseline is unknown.

    COST_HISTORY_INSUFFICIENT is the calendar gate (a young epoch; the re-evaluation window says when it clears);
    BILLED_COST_DAYS_INSUFFICIENT is a mature epoch billed on too few days (not time-boxed); DBU_COST_UNAVAILABLE is a
    cost that cannot be priced or a non-positive run rate.
    """
    if e.cost_history_days < t.min_economics_cost_history_days:
        return "COST_HISTORY_INSUFFICIENT"
    status = (e.forecast_metadata or {}).get("dbu_forecast_status")
    return "BILLED_COST_DAYS_INSUFFICIENT" if status in ("INSUFFICIENT_ACTIVE_COST_DAYS", "INSUFFICIENT_BILLING_CYCLES") else "DBU_COST_UNAVAILABLE"


MIGRATION_BASELINE_RATIONALE = {
    "COST_HISTORY_INSUFFICIENT": "Migration is judged on a pre-canary cost comparison, but the current epoch has fewer than 7 days of billing history to price it.",
    "BILLED_COST_DAYS_INSUFFICIENT": "Migration is judged on a pre-canary cost comparison, but the current epoch has fewer than 7 billed days to forecast its DBU cost.",
    "DBU_COST_UNAVAILABLE": "Migration is judged on a pre-canary cost comparison, but the current DBU cost is unavailable (unpriced usage or a non-positive run rate).",
}


DBU_BASELINE_WITHHELD = {
    # blocker -> (bundle status, reason shown on withheld plan economics)
    "COST_HISTORY_INSUFFICIENT": ("REQUIRES_COST_HISTORY", "At least one step cannot be priced because the current epoch has too little billing history; economics are withheld until it accrues."),
    "BILLED_COST_DAYS_INSUFFICIENT": ("UNAVAILABLE", "The current epoch has too few billed days to forecast its DBU cost; economics are withheld."),
    "DBU_COST_UNAVAILABLE": ("UNAVAILABLE", "The forward DBU cost is unavailable (unpriced usage or a non-positive run rate); economics are withheld."),
}


RESOURCE_WINDOW_BLOCKERS = {"MISSING": "RECENT_RESOURCE_WINDOW_UNAVAILABLE", "IDLE": "NO_RECENT_RUNNING_TIME"}
