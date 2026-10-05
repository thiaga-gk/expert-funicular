"""Configuration loading: turn parsed CLI arguments into a PublisherRequest."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Tuple

from publisher.contracts.request import MODE_DEMO, MODE_LIVE, MODE_REPLAY, PublisherRequest


def _resolve_execution_args(args: argparse.Namespace) -> Tuple[str,str]:
    supplied=[("warehouse",args.serverless_warehouse_id),("warehouse",args.sql_warehouse_id),("cluster",args.all_purpose_cluster_id)]
    supplied=[x for x in supplied if x[1]]
    if args.execution_id:
        if supplied: raise SystemExit("Use either --execution-id/--execution-kind or one convenience execution-ID flag, not both.")
        if not args.execution_kind: raise SystemExit("--execution-kind is required with --execution-id")
        return args.execution_kind,str(args.execution_id)
    if len(supplied)>1: raise SystemExit("Supply exactly one execution target: serverless/sql warehouse ID or all-purpose cluster ID.")
    if supplied: return supplied[0][0],str(supplied[0][1])
    raise SystemExit("Execution target required: --serverless-warehouse-id, --sql-warehouse-id, --all-purpose-cluster-id, or --execution-kind/--execution-id")


def request_from_args(args: argparse.Namespace) -> PublisherRequest:
    """The run request: demo when --demo, replay when --from-evidence, otherwise live with a resolved execution target."""
    common = dict(output_dir=Path(args.output_dir), batch_size=args.batch_size, generate_workbook=not args.no_workbook,
                  print_json=args.print_json, source_region=args.source_region)
    if args.demo:
        return PublisherRequest(mode=MODE_DEMO, demo_count=args.demo_count, **common)
    if args.from_evidence:
        return PublisherRequest(mode=MODE_REPLAY, evidence_files=tuple(args.from_evidence), **common)
    kind, eid = _resolve_execution_args(args)
    return PublisherRequest(
        mode=MODE_LIVE, profile=args.profile, execution_kind=kind, execution_id=eid, config_file=args.config_file,
        workspace_id=args.workspace_id, aws_price_registry=args.aws_price_registry,
        compatibility_api_mode=args.compatibility_api_mode, billing_ingested_through=args.billing_ingested_through,
        allow_missing_aws_price=args.allow_missing_aws_price, **common,
    )
