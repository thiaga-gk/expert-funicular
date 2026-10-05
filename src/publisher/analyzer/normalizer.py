"""Normalization of source values: sizes, owners, tags, Photon observation and query-source profiles."""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Optional

from publisher.common.time import iso, parse_datetime
from publisher.common.utils import as_string_list, clamp, safe_float, safe_int
from publisher.config.thresholds import Thresholds
from publisher.contracts.source import PhotonObservation, QuerySourceProfile


def is_nonprod(tags: Mapping[str, Any], t: Thresholds) -> bool:
    """Nonprod profile by warehouse tag: a known environment key (case-insensitive) with a known nonprod value."""
    keys, values = set(t.nonprod_tag_keys), set(t.nonprod_tag_values)
    return any(str(k).strip().lower() in keys and str(v).strip().lower() in values for k, v in (tags or {}).items())


def normalize_size(value: Any) -> str:
    return "UNKNOWN" if value is None else str(value).strip().upper().replace("-", "_")


def hive_reference_count(q: Mapping[str, Any]) -> Optional[int]:
    """Queries naming hive_metastore, or None when no statement text was visible (a zero would claim a clean scan)."""
    if safe_float(q.get("statement_text_visible_pct_30")) <= 0:
        return None
    return safe_int(q.get("hive_metastore_reference_queries_30"))


REDACTED_OWNER_VALUES = frozenset({"__REDACTED__", "<REDACTED>", "REDACTED"})


def clean_owner(value: Any) -> Optional[str]:
    """An owner identity, or None when it is blank or a redaction token (unknown, never an owner)."""
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.upper() in REDACTED_OWNER_VALUES else text


def infer_photon_observation(row: Mapping[str, Any], analysis_time: str, thresholds: Thresholds) -> PhotonObservation:
    """Infer observed Photon state from signed current-epoch DBU billing usage.

    Corrections remain safe because system.billing.usage retractions carry negative
    quantities and restatements carry corrected positive quantities. The inference
    is intentionally conservative: insufficient coverage, stale evidence, or a
    mixed workload produces UNKNOWN/MIXED instead of pretending to know config.
    """
    true_dbu = max(0.0, safe_float(row.get("photon_true_dbu")))
    false_dbu = max(0.0, safe_float(row.get("photon_false_dbu")))
    null_dbu = max(0.0, safe_float(row.get("photon_null_dbu")))
    known = true_dbu + false_dbu
    total = known + null_dbu
    coverage = (100.0 * known / total) if total > 0 else 0.0
    enabled_pct = (100.0 * true_dbu / known) if known > 0 else None
    latest = iso(row.get("latest_usage_time"))
    latest_dt = parse_datetime(latest) if latest else None
    as_of = parse_datetime(analysis_time)
    freshness_days = None
    if latest_dt and as_of:
        freshness_days = max(0, (as_of.astimezone(dt.timezone.utc) - latest_dt.astimezone(dt.timezone.utc)).days)

    sufficient = (
        total >= thresholds.photon_min_epoch_dbu
        and coverage >= thresholds.photon_min_feature_coverage_pct
        and freshness_days is not None
        and freshness_days <= thresholds.photon_freshness_days
        and enabled_pct is not None
    )
    if not sufficient:
        status = "UNKNOWN"
    elif enabled_pct >= thresholds.photon_enabled_usage_pct:
        status = "ENABLED"
    elif enabled_pct <= thresholds.photon_disabled_usage_pct:
        status = "DISABLED"
    else:
        status = "MIXED"

    freshness_score = 0.0 if freshness_days is None else clamp(1.0 - freshness_days / max(1.0, float(thresholds.photon_freshness_days)), 0.0, 1.0)
    coverage_score = clamp(coverage / 100.0, 0.0, 1.0)
    volume_score = clamp(total / max(thresholds.photon_min_epoch_dbu * 10.0, 1.0), 0.0, 1.0)
    purity = 0.0 if enabled_pct is None else max(enabled_pct, 100.0 - enabled_pct) / 100.0
    score = clamp(0.35 * coverage_score + 0.25 * freshness_score + 0.20 * volume_score + 0.20 * purity, 0.0, 1.0)
    if status == "UNKNOWN":
        score = min(score, 0.49)
    elif status == "MIXED":
        score = min(score, 0.59)
    confidence = {
        "score": round(score, 3),
        "level": "HIGH" if score >= thresholds.confidence_high_threshold else "MEDIUM" if score >= thresholds.confidence_medium_threshold else "LOW",
        "components": {
            "feature_coverage": round(coverage_score, 3),
            "freshness": round(freshness_score, 3),
            "dbu_volume": round(volume_score, 3),
            "state_purity": round(purity, 3),
        },
    }
    return PhotonObservation(
        status=status,
        enabled_usage_pct=(round(enabled_pct, 3) if enabled_pct is not None else None),
        known_feature_coverage_pct=round(coverage, 3),
        true_dbu=round(true_dbu, 6),
        false_dbu=round(false_dbu, 6),
        null_dbu=round(null_dbu, 6),
        latest_usage_time=latest,
        freshness_days=freshness_days,
        confidence=confidence,
    )


def derive_query_source_profile(query: Mapping[str, Any]) -> QuerySourceProfile:
    """Create a deterministic multi-label source profile from query history aggregates."""
    categories = {
        "DASHBOARD": safe_int(query.get("dashboard_queries_30")),
        "JOB": safe_int(query.get("job_queries_30")),
        "NOTEBOOK": safe_int(query.get("notebook_queries_30")),
        "GENIE": safe_int(query.get("genie_queries_30")),
        "ALERT": safe_int(query.get("alert_queries_30")),
        "SQL_EDITOR": safe_int(query.get("sql_query_source_queries_30")),
        "POWER_BI": safe_int(query.get("power_bi_queries_30")),
        "TABLEAU": safe_int(query.get("tableau_queries_30")),
        "LOOKER": safe_int(query.get("looker_queries_30")),
    }
    q30 = max(0, safe_int(query.get("queries_30")))
    percentages = {k: round(100.0 * v / q30, 3) if q30 else 0.0 for k, v in categories.items()}
    ranked = sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))
    primary = ranked[0][0] if ranked and ranked[0][1] > 0 else "UNATTRIBUTED"
    apps = as_string_list(query.get("client_applications_30")) or []
    return QuerySourceProfile(
        categories=categories, percentages=percentages, primary_category=primary,
        client_applications=sorted({str(x) for x in apps if x is not None}), multi_label=True,
    )
