"""Replay evidence files (evidence.json): versioned envelope, schema validation, load/merge and save.

Evidence holds owner emails and tags, so nothing from the payload is logged: only file and warehouse counts, and
workspace/warehouse IDs. There are no legacy shims: other format versions and bare lists are rejected.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from publisher.analyzer.collector.billing import _is_unpriced_only_row
from publisher.analyzer.normalizer import clean_owner
from publisher.common.errors import PublisherError
from publisher.common.hashing import canonical_json
from publisher.common.logging import LOGGER
from publisher.common.utils import as_serializable, round_or_none, safe_float, safe_int
from publisher.config.settings import _EVIDENCE_SCHEMA_PATH
from publisher.config.versions import EVIDENCE_FORMAT_VERSION, PUBLISHER_VERSION
from publisher.contracts.source import (
    EpochEvidence,
    PhotonObservation,
    QuerySourceProfile,
    WarehouseAttribution,
    WarehouseConfig,
    WarehouseEvidence,
)


def validate_evidence_payload(payload: Any) -> None:
    """Fail closed unless the payload is an evidence file of exactly EVIDENCE_FORMAT_VERSION that matches the schema.

    The message names the failing path and rule, never the offending value: evidence carries owner emails and tags.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(f"evidence file must be an object with evidence_format_version {EVIDENCE_FORMAT_VERSION}")
    version = payload.get("evidence_format_version")
    if version != EVIDENCE_FORMAT_VERSION:
        raise ValueError(f"unsupported evidence_format_version {version!r}; this publisher reads only {EVIDENCE_FORMAT_VERSION}")
    try:
        import jsonschema  # type: ignore
    except ImportError as exc:
        raise RuntimeError("jsonschema>=4.23 is required to validate evidence files") from exc
    schema = json.loads(_EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    error = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(schema).iter_errors(dict(payload)))
    if error is not None:
        where = "/".join(str(x) for x in error.absolute_path) or "<file>"
        rule = error.message if error.validator == "required" else f"fails '{error.validator}'"
        raise ValueError(f"evidence file invalid at {where}: {rule}")


def save_evidence(path:Path,evidence:Sequence[WarehouseEvidence])->None:
    """Write the evidence envelope to ``path`` as sorted-key JSON, validated against the schema before writing.

    A payload that fails validation raises before any file content is written. The file is sensitive (owner emails, tags).
    """
    payload = {"evidence_format_version": EVIDENCE_FORMAT_VERSION, "publisher_version": PUBLISHER_VERSION, "warehouses": as_serializable(evidence)}
    validate_evidence_payload(payload)
    path.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    LOGGER.info("replay.save warehouses=%d evidence_format_version=%s", len(evidence), EVIDENCE_FORMAT_VERSION)


def _epoch_from_dict(x:Mapping[str,Any])->EpochEvidence:
    """Rebuild an EpochEvidence from its serialized dict; ``resource_daily`` None (not collected) stays distinct from []."""
    return EpochEvidence(epoch_id=str(x["epoch_id"]),warehouse_id=str(x["warehouse_id"]),workspace_id=str(x.get("workspace_id") or ""),account_id=str(x.get("account_id") or ""),warehouse_name=str(x.get("warehouse_name") or x["warehouse_id"]),
                         epoch_start=str(x["epoch_start"]),epoch_end=x.get("epoch_end"),is_current=bool(x.get("is_current")),config=WarehouseConfig(**x["config"]),tags=dict(x.get("tags") or {}),query=dict(x.get("query") or {}),events=dict(x.get("events") or {}),gaps=dict(x.get("gaps") or {}),cost=dict(x.get("cost") or {}),
                         resource_daily=None if x.get("resource_daily") is None else sorted((dict(r) for r in x["resource_daily"]), key=lambda r: str(r.get("usage_day"))))


def evidence_from_dict(x: Mapping[str, Any]) -> WarehouseEvidence:
    """Rebuild a WarehouseEvidence from its serialized dict (already schema-validated by the caller).

    Missing optional parts stay None/empty rather than defaulted to a value (photon_observation, attribution). Owners
    are re-cleaned so a replayed file cannot reintroduce a non-owner identity.
    """
    return WarehouseEvidence(
        warehouse_id=str(x["warehouse_id"]),
        workspace_id=str(x.get("workspace_id") or ""),
        account_id=str(x.get("account_id") or ""),
        warehouse_name=str(x.get("warehouse_name") or x["warehouse_id"]),
        source_region=str(x.get("source_region") or "unknown"),
        analysis_time=str(x.get("analysis_time") or ""),
        current_epoch_id=str(x["current_epoch_id"]),
        epochs=[_epoch_from_dict(e) for e in x.get("epochs", [])],
        current_cost_365=None if x.get("current_cost_365") is None else safe_float(x.get("current_cost_365")),
        baseline_forecast_cost_365=None if x.get("baseline_forecast_cost_365") is None else safe_float(x.get("baseline_forecast_cost_365")),
        currency=str(x.get("currency") or "USD"),
        cost_history_days=safe_int(x.get("cost_history_days")),
        cost_cv_84=None if x.get("cost_cv_84") is None else safe_float(x.get("cost_cv_84")),
        forecast_metadata=dict(x.get("forecast_metadata") or {}),
        daily_cost_365=list(x.get("daily_cost_365") or []),
        billing_features=dict(x.get("billing_features") or {}),
        aws_baseline_cost_365=(None if x.get("aws_baseline_cost_365") is None else safe_float(x.get("aws_baseline_cost_365"))),
        aws_cost_model=dict(x.get("aws_cost_model") or {}),
        sql_sku_prices=dict(x.get("sql_sku_prices") or {}),
        compatibility_evidence=dict(x.get("compatibility_evidence") or {}),
        photon_observation=(PhotonObservation(**x["photon_observation"]) if x.get("photon_observation") else None),
        attribution=(WarehouseAttribution(
            owner=clean_owner((x.get("attribution") or {}).get("owner")),
            observed_owners=sorted({o for o in (clean_owner(v) for v in (x.get("attribution") or {}).get("observed_owners") or []) if o is not None}),
            owner_source=str((x.get("attribution") or {}).get("owner_source") or "system.billing.usage.identity_metadata.owned_by"),
            query_source=QuerySourceProfile(**((x.get("attribution") or {}).get("query_source") or {"categories":{},"percentages":{},"primary_category":"UNATTRIBUTED","client_applications":[],"multi_label":True})),
        ) if x.get("attribution") else None),
    )


def load_evidence_files(paths: Sequence[str]) -> List[WarehouseEvidence]:
    """Merge regional evidence with composite workspace/warehouse identity.

    Exact overlaps are de-duplicated. Conflicting observations for the same
    workspace/warehouse/epoch or daily cost key fail loudly rather than being
    averaged or silently preferred. Warehouse IDs may legitimately collide
    across workspaces, so workspace_id is always part of the identity.
    """
    merged: Dict[Tuple[str, str], WarehouseEvidence] = {}
    duplicates = 0
    LOGGER.info("replay.load start files=%d", len(paths))

    for filename in paths:
        try:
            payload = json.loads(Path(filename).read_text(encoding="utf-8"))
            validate_evidence_payload(payload)
        except ValueError as exc:
            raise PublisherError(f"{filename}: {exc}") from exc
        for item in payload["warehouses"]:
            e = evidence_from_dict(item)
            key = (e.workspace_id, e.warehouse_id)
            if not e.analysis_time:
                raise PublisherError(f"Evidence is missing analysis_time for {key}")

            if key not in merged:
                merged[key] = e
                continue

            prior = merged[key]
            duplicates += 1
            LOGGER.debug("replay.merge workspace_id=%s warehouse_id=%s", key[0], key[1])
            # Top-level modeled values must describe the same observation cut.
            comparable = (
                prior.analysis_time, prior.current_epoch_id, prior.currency,
                round_or_none(prior.current_cost_365, 6), round_or_none(prior.baseline_forecast_cost_365, 6),
                prior.cost_history_days, round_or_none(prior.cost_cv_84, 6), canonical_json(prior.forecast_metadata),
                None if prior.aws_baseline_cost_365 is None else round(prior.aws_baseline_cost_365, 6),
                canonical_json(prior.aws_cost_model), canonical_json(prior.sql_sku_prices),
                canonical_json(prior.photon_observation), canonical_json(prior.attribution),
            )
            incoming = (
                e.analysis_time, e.current_epoch_id, e.currency,
                round_or_none(e.current_cost_365, 6), round_or_none(e.baseline_forecast_cost_365, 6),
                e.cost_history_days, round_or_none(e.cost_cv_84, 6), canonical_json(e.forecast_metadata),
                None if e.aws_baseline_cost_365 is None else round(e.aws_baseline_cost_365, 6),
                canonical_json(e.aws_cost_model), canonical_json(e.sql_sku_prices),
                canonical_json(e.photon_observation), canonical_json(e.attribution),
            )
            if comparable != incoming:
                raise PublisherError(f"Conflicting modeled evidence for workspace/warehouse {key}")

            epochs: Dict[str, EpochEvidence] = {x.epoch_id: x for x in prior.epochs}
            for ep in e.epochs:
                if ep.epoch_id in epochs and canonical_json(epochs[ep.epoch_id]) != canonical_json(ep):
                    raise PublisherError(f"Conflicting epoch evidence for {key}/{ep.epoch_id}")
                epochs.setdefault(ep.epoch_id, ep)

            daily: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            for row in prior.daily_cost_365 + e.daily_cost_365:
                currency_key = "UNPRICED" if _is_unpriced_only_row(row) else str(row.get("currency_code") or prior.currency)
                dk = (str(row.get("usage_date")), str(row.get("epoch_start")), currency_key)
                if dk in daily and canonical_json(daily[dk]) != canonical_json(row):
                    raise PublisherError(f"Conflicting daily cost evidence for {key}/{dk}")
                daily[dk] = row

            features = dict(prior.billing_features)
            for fk, fv in e.billing_features.items():
                if fk in features and canonical_json(features[fk]) != canonical_json(fv):
                    raise PublisherError(f"Conflicting billing feature evidence for {key}/{fk}")
                features[fk] = fv

            regions = sorted({x for x in (prior.source_region.split(",") + e.source_region.split(",")) if x})
            merged[key] = WarehouseEvidence(
                warehouse_id=prior.warehouse_id, workspace_id=prior.workspace_id, account_id=prior.account_id or e.account_id,
                warehouse_name=prior.warehouse_name, source_region=",".join(regions), analysis_time=prior.analysis_time,
                current_epoch_id=prior.current_epoch_id, epochs=sorted(epochs.values(), key=lambda x: x.epoch_start),
                current_cost_365=prior.current_cost_365, baseline_forecast_cost_365=prior.baseline_forecast_cost_365,
                currency=prior.currency, cost_history_days=prior.cost_history_days, cost_cv_84=prior.cost_cv_84,
                forecast_metadata=dict(prior.forecast_metadata), daily_cost_365=list(daily.values()), billing_features=features,
                aws_baseline_cost_365=prior.aws_baseline_cost_365, aws_cost_model=dict(prior.aws_cost_model), sql_sku_prices=dict(prior.sql_sku_prices),
                compatibility_evidence=dict(prior.compatibility_evidence or e.compatibility_evidence),
                photon_observation=prior.photon_observation or e.photon_observation,
                attribution=prior.attribution or e.attribution,
            )

    LOGGER.info("replay.load done files=%d warehouses=%d merged_overlaps=%d", len(paths), len(merged), duplicates)
    return sorted(merged.values(), key=lambda e: (e.warehouse_name.lower(), e.workspace_id, e.warehouse_id))
