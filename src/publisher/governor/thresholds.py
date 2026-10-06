"""Policy parameters (Thresholds): every decision default the policy engine applies, documented in docs/reference/thresholds.md."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from publisher.config.defaults import GIB


@dataclass(frozen=True)
class Thresholds:
    """Central deterministic policy, evidence, confidence, and estimator thresholds (frozen; the Governor owns them).

    Every field has a row in docs/reference/thresholds.md. A change here is decision-affecting: bump the affected
    component versions and regenerate goldens. Units are in the field name or its comment (pct, ms, minutes, days).
    The defaults are the balanced cost profile; governor/cost_profiles.py derives the other profiles by replacing the
    risk-appetite fields only.
    """
    # Evidence sufficiency / epoch stability
    min_query_sample_size: int = 100
    strong_query_sample_size: int = 500
    min_cost_history_days: int = 14
    # Hard floor: below this many current-epoch billing days no forecast-dependent estimate is published, a cost-judged
    # migration is INSUFFICIENT_EVIDENCE and an approval-only cost change waits for its price (cost history restarts
    # with each applied change). It can trail epoch age by up to a day when the billing watermark has not yet covered
    # the latest day.
    min_economics_cost_history_days: int = 7
    forecast_cadence_gap_days: int = 7  # short runs resting longer than this between them are a periodic workload
    forecast_min_cycles: int = 2  # whole run-to-run cycles the cadence forecast needs
    forecast_run_bridge_days: int = 3  # billed days this close belong to one run (a weekend inside a month-end close)
    readiness_min_billed_days: int = 2  # settled billed days the current configuration needs before a change is decided
    canary_p95_regression_pct: float = 25.0  # p95 execution rise on shared statement sources that rolls a canary back
    canary_p95_min_queries: int = 300  # executed queries on shared sources the p95 rollback guard needs

    # Photon inference from signed DBU billing evidence in the current warehouse epoch.
    photon_min_feature_coverage_pct: float = 90.0
    photon_enabled_usage_pct: float = 95.0
    photon_disabled_usage_pct: float = 5.0
    photon_min_epoch_dbu: float = 1.0
    photon_freshness_days: int = 14

    # Queue / concurrency
    queue_query_pct_increase_max: float = 5.0
    queue_wait_p95_ms_increase_max: int = 30_000
    queue_query_pct_safe: float = 0.5
    queue_wait_p95_ms_safe: int = 5_000

    # Size / spill / latency
    spill_query_pct_upsize: float = 2.0
    spill_p95_bytes_upsize: int = GIB
    # Severity percentiles are taken among affected queries; rules that add capacity/cost need a p95
    # backed by at least this many affected queries (below ~20 samples a p95 is just the maximum).
    severity_min_affected_queries: int = 20
    spill_query_pct_downsize_safe: float = 0.1
    # A few statements carrying most spilled bytes are a rewrite, not an upsize for every query.
    spill_concentration_top_sources: int = 3
    spill_concentration_hold_pct: float = 80.0
    # Failed/cancelled share of queries; above it, low spill/queue/latency is not trustworthy evidence of headroom.
    failure_cancel_pct_downsize_safe: float = 5.0
    p95_execution_ms_downsize_safe: int = 5_000
    p99_execution_ms_downsize_safe: int = 30_000
    # Batch profile: a warehouse whose recent queries come mostly from jobs is judged on run days, not on interactive
    # latency (a 10-minute ETL query is normal), for a downsize canary only.
    batch_job_query_share_pct: float = 80.0
    batch_min_active_days: int = 20

    # Min / max clusters
    time_at_min_pct_reduce: float = 25.0
    time_at_max_pct_block_min_reduce: float = 20.0
    time_at_max_pct_safe_reduce: float = 1.0
    # Saturation = real time at the configured ceiling. A peak above max is not evidence (cluster recycling can briefly exceed it).
    time_at_max_pct_saturated: float = 5.0
    # A ceiling must leave growth room above an observed peak: max(clusters, ceil(pct x peak)). The size-down hold for a
    # peak outside the recent window uses max_cluster_headroom_pct; the Serverless max-cluster cut uses the cost-profiled
    # max_cluster_cut_headroom_pct.
    max_cluster_headroom_clusters: int = 1
    max_cluster_headroom_pct: float = 25.0
    max_cluster_cut_headroom_pct: float = 25.0
    # Concurrency-derived cluster targets (Pro/Classic queries per cluster: config/defaults.py).
    max_cluster_target_percentile: float = 99.5  # of busy minutes' running + queued queries (recent window and whole epoch)
    max_cluster_target_headroom_clusters: int = 1
    min_cluster_busy_start_percentile: float = 95.0  # of busy periods' peak demand in their first scale-up-lag minutes
    concurrency_min_busy_minutes: int = 200  # a P99.5 needs at least this many busy minutes
    # Long windows (the whole current epoch, older epochs) measure peak demand as the level reached in at least this
    # many busy minutes: a percentile over months dilutes a two-hour quarter-end peak below its tail.
    peak_demand_min_minutes: int = 30
    min_cluster_min_busy_periods: int = 20  # a P95 of busy-period starts needs at least this many periods

    # Auto-stop
    min_cluster_minutes_for_cost_model: float = 1.0
    # Auto-stop values evaluated: every step from the type's floor up to the maximum (floors and maximum: config/defaults.py).
    auto_stop_step_minutes: int = 5
    # A value covers an idle gap (keeps the warehouse up until the next query) only when it is at least this much longer
    # than the gap, so a small schedule drift does not turn every gap into a restart.
    auto_stop_bridge_margin_pct: float = 10.0
    # A gap counts as running throughout (a lower auto-stop adds a restart there) when the warehouse ran at least this
    # fraction of it; the slack absorbs event-time rounding.
    running_idle_full_window_fraction: float = 0.99
    # A gap counts as kept alive (by sessions, result fetching or keep-alive, not queries) when the warehouse ran this
    # many minutes past the current auto-stop; below it, the stop event merely trailed the auto-stop.
    kept_alive_gap_tolerance_minutes: float = 2.0
    # A gap the warehouse stopped in before the next query is kept alive only when it ran longer than this past the
    # auto-stop (a session held it open); shorter overruns are a late stop event.
    kept_alive_stopped_gap_minutes: float = 10.0
    # Cost of one restart in cluster-minutes per cluster started, from the documented start times: Pro/Classic about 4
    # minutes, Serverless 2-6 seconds (upper end). Whether starting time is billed is not documented, so this is a
    # priced assumption.
    restart_cost_cluster_minutes_pro_classic: float = 4.0
    restart_cost_cluster_minutes_serverless: float = 0.1
    # Cold-start budget: restarts a lower auto-stop adds, as a share of queries over the same window (Pro/Classic only;
    # cost-profiled, so a nonprod warehouse on the aggressive profile gets the larger budget).
    cold_start_added_share_max_pct: float = 2.0
    # Among auto-stop values whose net saving is within this share of the best, the smallest change is chosen.
    auto_stop_near_best_pct: float = 2.0
    # Nonprod tag: a warehouse tag whose key (case-insensitive) is one of these keys and whose value is one of these
    # values selects the aggressive cost profile by default. Staging and UAT stay production.
    nonprod_tag_keys: Tuple[str, ...] = ("env", "environment", "stage", "tier")
    nonprod_tag_values: Tuple[str, ...] = ("dev", "development", "test", "testing", "qa", "sandbox", "nonprod", "non-prod")
    # Non-work activity flags (observations; nothing is priced from them): running time after the current auto-stop
    # with no query running at or above this share of running time; and a warehouse running at least this share of
    # the clock while queries are busy at most this share of its running time, with no auto-stop value removing it.
    kept_alive_flag_pct: float = 10.0
    always_on_running_pct: float = 90.0
    always_on_busy_pct_max: float = 10.0

    # Forecast
    forecast_recent_days: int = 28
    forecast_long_days: int = 84
    billing_settle_hours: int = 24  # a UTC day's billing counts only this long after the day ends
    pressure_window_days: int = 30  # recent window for capacity facts (time at min/max, observed peak)
    forecast_cv_min_days: int = 7
    forecast_weight_28_long: float = 0.65
    forecast_weight_28_mid: float = 0.75
    trend_adjustment_weight: float = 0.25
    trend_ratio_floor: float = 0.75
    trend_ratio_ceiling: float = 1.25

    # Scenario uncertainty / realization
    realization_conservative: float = 0.50
    realization_base: float = 0.75
    realization_optimistic: float = 1.00
    min_cluster_savings_fraction_cap: float = 0.50
    # Time at the min-cluster floor shows the autoscaler sat at the floor, not that a floor cluster was
    # spare. The time-at-min figure is therefore an upper bound on removable capacity (optimistic = 100%),
    # the floor capacity may have been needed (conservative = 0%), and base is the midpoint (audit H4).
    min_cluster_realization_optimistic: float = 1.00
    min_cluster_realization_base: float = 0.50
    min_cluster_realization_conservative: float = 0.00
    max_cluster_base_uplift_weight: float = 0.50
    downsize_runtime_multiplier_conservative: float = 2.0  # fully linear slowdown: one size down halves the workers
    downsize_runtime_multiplier_base: float = 1.30
    downsize_runtime_multiplier_optimistic: float = 1.10
    upsize_runtime_multiplier_conservative: float = 1.00
    upsize_runtime_multiplier_base: float = 0.70
    upsize_runtime_multiplier_optimistic: float = 0.50
    # Pro/Classic DBU per cluster-hour by size is not documented first-party: a workspace's billed rates price a size
    # change only when they rise with size and the pair ratio is within this band of the published size ratio.
    size_rate_published_band: float = 0.25
    # A change made to save money ships only when its own base modeled savings are positive and at least this much a
    # year (warehouse currency).
    cost_change_min_annual_savings: float = 500.0
    # Migration is priced relative to billing: the same cost model applied to the current warehouse must land within
    # this band of its billed DBU forecast, or the migration estimate is withheld.
    migration_reconciliation_min: float = 0.70
    migration_reconciliation_max: float = 1.30
    cost_factor_floor: float = 0.05
    cost_factor_ceiling: float = 3.00
    confidence_coverage_days: float = 90.0
    confidence_epoch_maturity_days: float = 30.0
    confidence_base_weight: float = 0.10
    confidence_coverage_weight: float = 0.25
    confidence_sample_weight: float = 0.25
    confidence_stability_weight: float = 0.15
    confidence_epoch_weight: float = 0.15
    confidence_signal_weight: float = 0.10
    confidence_high_threshold: float = 0.80
    confidence_medium_threshold: float = 0.60
    confidence_signal_change: float = 1.00
    confidence_signal_no_change: float = 0.70
    confidence_cap_test_required: float = 0.78
    confidence_cap_actionable: float = 0.93
    # Full credit of the sample component for the knobs that do not decide on a query count (5x their evidence minimum).
    confidence_strong_busy_minutes: int = 1000
    confidence_strong_busy_periods: int = 100
    remeasure_days_after_invalidating_change: int = 7
    growth_hold_weekly_ratio: float = 1.10  # capacity cuts wait while every one of the last 4 weeks has >= this x the prior 4-week median of queries
    plan_risk_hitl_change: float = 1.0  # plan order divides base savings x confidence by this for an ACTIONABLE step
    plan_risk_canary_change: float = 2.0  # ... and by this for a TEST_REQUIRED step (a canary costs a measurement cycle)

    def __post_init__(self) -> None:
        weights = (self.confidence_base_weight, self.confidence_coverage_weight, self.confidence_sample_weight,
                   self.confidence_stability_weight, self.confidence_epoch_weight, self.confidence_signal_weight)
        if abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError(f"confidence weights must sum to 1.0, got {sum(weights)}")

    # Caps derived from the confidence cut-offs, so a cap can never drift above the level it keeps a candidate below.
    @property
    def confidence_cap_default(self) -> float:
        """Every status but ACTIONABLE/TEST_REQUIRED/INSUFFICIENT_EVIDENCE: just below HIGH, so a decision that changes nothing is never HIGH."""
        return round(self.confidence_high_threshold - 0.01, 2)

    @property
    def confidence_cap_insufficient_evidence(self) -> float:
        """Just below the MEDIUM cut-off: a candidate without enough evidence is LOW."""
        return round(self.confidence_medium_threshold - 0.01, 2)

    @property
    def confidence_cap_serverless_ceiling_increase(self) -> float:
        """Serverless scales differently from the documented Pro/Classic rule, so its queue-based ceiling increase is
        shown LOW (just below the MEDIUM cut-off)."""
        return round(self.confidence_medium_threshold - 0.01, 2)
