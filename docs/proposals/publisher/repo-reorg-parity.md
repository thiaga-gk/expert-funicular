# Repository reorganization: parity record

Status: **Complete.** The single-file publisher (2.10.0, tag `reorg-oracle-2.10.0`) is retired as the behavioral oracle; the capability packages under `src/publisher/` are the only implementation. Design and approved decisions: [`repo-reorg-design.md`](../architecture/repo-reorg-design.md) section 21.

## Behavioral parity

Every phase of the reorganization merged only with these results, and the final tree reproduces them:

| Check | Result |
|---|---|
| Canonical outputs against the single-file tag (`scripts/compare_with_ref.py --ref reorg-oracle-2.10.0`): the 41-warehouse demo fleet and the 23 evidence-level golden cases through the CLI | 24 runs, 0 canonical diffs (packages, `evidence.json`, `manifest.json`, workbook cell dump) |
| Stored baseline (`tests/fixtures/expected/demo/`, every `tests/golden/cases/*/expected/`, committed schema and contract example digests) | identical |
| Test suite | 665 tests (the original 608 plus the baseline, unit and component tests added during the reorganization; 3 live checks skip without a Databricks profile), all pass |
| Demo, workbook validator, contract example, skill suite | pass |

The run log is not canonical output. It now groups lines by stage (all eligible warehouses are optimized before any is reviewed and planned), so one line, `pipeline.batch.done`, precedes the per-warehouse `warehouse.decisions` lines.

## Definition map

245 of the 246 top-level definitions of the single-file `publisher.py` exist in the package under the same name, and every method of its classes exists on the same class or one of its mixins (`Estimator` with `CostModel` and `SavingsModel`, `Reviewer` with `CandidateGate` and `ScenarioGate`, `ScenarioPlanner` with `PlanOrdering`). `Optimizer.optimize` calls one technique function per knob (`optimizer/techniques/`) and `optimizer/conflicts.py`, whose statements are the method's original sections. The remaining one has no single counterpart: `optimize_warehouse` is the composition `optimizer.optimize` -> `reviewer.review_candidates` -> `packager.plan` -> `reviewer.review_plans`.

| Module | Definitions from the single-file publisher |
|---|---|
| `src/publisher/config/defaults.py` | `GIB`, `SIZE_ORDER`, `SQL_DBU_PER_CLUSTER_HOUR`, `AWS_SQL_WAREHOUSE_TOPOLOGY`, `SUPPORTED_WAREHOUSE_TYPES`, `ACTIVE_KNOBS` |
| `src/publisher/cli/publish.py` | `parse_args`, `_resolve_execution_args`, and the old `main` as `run` |
| `src/publisher/config/settings.py` | `_CONTRACT_SCHEMA_PATH`, `_EVIDENCE_SCHEMA_PATH` |
| `src/publisher/config/versions.py` | `PUBLISHER_VERSION`, `CONTRACT_VERSION`, `POLICY_VERSION`, `ANALYZER_VERSION`, `OPTIMIZER_VERSION`, `MODEL_VERSION`, `ESTIMATOR_VERSION`, `BUNDLE_MODEL_VERSION`, `REVIEWER_VERSION`, `DECISION_VERSION`, `TECHNIQUE_CATALOG_VERSION`, `EVIDENCE_FORMAT_VERSION` |
| `src/publisher/common/hashing.py` | `canonical_json` |
| `src/publisher/common/ids.py` | `epoch_id`, `finding_id`, `recommendation_id`, `scenario_id` |
| `src/publisher/common/logging.py` | `LOGGER`, `setup_logging` |
| `src/publisher/common/time.py` | `parse_datetime`, `iso`, `_iso_z` |
| `src/publisher/common/utils.py` | `optional_float`, `safe_float`, `safe_int`, `round_or_none`, `clamp`, `_map_pairs`, `normalize_map`, `as_serializable`, `as_string_list` |
| `src/publisher/contracts/analysis.py` | `Finding`, `AnalysisResult` |
| `src/publisher/contracts/candidate.py` | `OptimizationDecision`, `Range3`, `CandidateRecommendation` |
| `src/publisher/contracts/review.py` | `WarehouseOptimization` |
| `src/publisher/contracts/scenario.py` | `PlanStep`, `PortfolioEstimate`, `Scenario`, `PRICING_AUTHORITATIVE`, `PRICING_CONTINGENT` |
| `src/publisher/contracts/source.py` | `WarehouseConfig`, `PhotonObservation`, `QuerySourceProfile`, `WarehouseAttribution`, `EpochEvidence`, `WarehouseEvidence` |
| `src/publisher/analyzer/analyzer.py` | `_is_supported_warehouse` |
| `src/publisher/analyzer/blockers.py` | `failure_blockers`, `dbu_baseline_blocker`, `MIGRATION_BASELINE_RATIONALE`, `DBU_BASELINE_WITHHELD`, `RESOURCE_WINDOW_BLOCKERS` |
| `src/publisher/analyzer/calibration.py` | `_billed_dbu_per_cluster_hour`, `_prepare_workspace_calibration` |
| `src/publisher/analyzer/collector/billing.py` | `_is_unpriced_only_row`, `settle_reference_time`, `billing_check_through`, `effective_billing_watermark`, `collect_unseen_billed_warehouses` |
| `src/publisher/analyzer/collector/collector.py` | `_warehouse_key`, `_row_key`, `_index_epoch_rows`, `extract_evidence` |
| `src/publisher/analyzer/collector/demo.py` | `_demo_daily`, `synthesize_resource_daily`, `_demo_interp`, `demo_evidence`, `DEMO_ANALYSIS_TIME`, `DEMO_ACCOUNT_ID`, `DEMO_WORKSPACES`, `DEMO_SKU_PRICES`, `DEMO_UNPRICED_SKU`, `DEMO_SKU_BY_TYPE`, `DEMO_START_LATENCY_MS`, `DEMO_SCALE_UP_LAG_MINUTES`, `DEMO_FLEET`, `_demo_unit`, `_demo_minutes`, `_demo_day_matches`, `_demo_config_at`, `_simulate_demo_warehouse`, `_DEMO_ROWS_CACHE`, `_demo_fleet_rows`, `synthetic_demo_evidence` |
| `src/publisher/analyzer/collector/pricing.py` | `classify_sql_sku`, `load_aws_price_registry`, `aws_cluster_hourly_price`, `model_aws_baseline_cost` |
| `src/publisher/analyzer/collector/queries/billing.py` | `EPOCH_DAILY_COST_SQL`, `BILLING_FEATURE_SQL`, `BILLING_WATERMARK_SQL`, `EPOCH_BILLING_ATTRIBUTION_SQL`, `SQL_SKU_PRICE_SQL`, `UNSEEN_BILLED_WAREHOUSES_SQL` |
| `src/publisher/analyzer/collector/queries/config_epochs.py` | `CONFIG_EPOCHS_CTE`, `CONFIG_EPOCHS_SQL` |
| `src/publisher/analyzer/collector/queries/events.py` | `EPOCH_EVENT_METRICS_SQL`, `build_epoch_gap_metrics_sql`, `RUNNING_IDLE_TARGET_FIELDS`, `RUNNING_IDLE_HEADER_FIELDS`, `_running_idle_ctes`, `build_running_idle_sql`, `build_running_idle_targets_sql`, `RESOURCE_DAILY_COLLECT_DAYS`, `RESOURCE_DAILY_SQL` |
| `src/publisher/analyzer/collector/queries/query_history.py` | `EPOCH_QUERY_METRICS_SQL`, `build_spill_source_sql`, `SPILL_SOURCE_SQL_TEMPLATE`, `CANARY_GUARD_FIELDS`, `CANARY_GUARD_SQL`, `PERIODS_CTE`, `CARRY_PERIOD_COLUMNS`, `build_carried_query_metrics_sql`, `build_carried_spill_source_sql`, `build_carried_concurrency_sql`, `CONCURRENCY_SCOPES`, `build_concurrency_sql` |
| `src/publisher/analyzer/collector/replay.py` | `validate_evidence_payload`, `save_evidence`, `_epoch_from_dict`, `evidence_from_dict`, `load_evidence_files` |
| `src/publisher/analyzer/collector/sql_executor.py` | `UTC_TIMEZONE_NAMES`, `SqlClient`, `with_ingestion_cutoff` |
| `src/publisher/analyzer/collector/workspace_api.py` | `AuthContext`, `_load_profile_values`, `_load_profile_host`, `_resolve_cli_token`, `_api_get`, `_current_workspace_id`, `_validate_all_purpose_cluster`, `resolve_cli_oauth`, `_external_hive_config_keys`, `collect_workspace_compatibility_evidence` |
| `src/publisher/analyzer/epochs.py` | `billed_days_recent`, `ready_billed_days`, `epoch_age_days`, `observed_epoch_calendar_days`, `DEMAND_CARRY_FIELDS`, `QUEUE_CARRY_FIELDS`, `DEMAND_CARRY_FINDINGS`, `QUEUE_CARRY_FINDINGS`, `carried_query_view` |
| `src/publisher/analyzer/findings.py` | `canary_observation`, `canary_rationale`, `Analyzer` |
| `src/publisher/analyzer/forecast.py` | `billing_cadence`, `Modeler` |
| `src/publisher/analyzer/metrics.py` | `auto_stop_targets`, `auto_stop_candidates`, `running_idle_metrics`, `concurrency_histograms`, `histogram_percentile`, `histogram_top_level`, `recent_peak_at_rate`, `window_days`, `cost_stability`, `conditional_severity`, `RESOURCE_SERIES_FIELDS`, `resource_window_from`, `resource_window`, `annualized_resource_rates`, `whole_weeks`, `blended_daily_rate`, `recent_capacity_facts`, `size_busy_share`, `query_volume_growth_ratio` |
| `src/publisher/analyzer/normalizer.py` | `is_nonprod`, `normalize_size`, `hive_reference_count`, `REDACTED_OWNER_VALUES`, `clean_owner`, `infer_photon_observation`, `derive_query_source_profile` |
| `src/publisher/governor/constraints.py` | `aws_region_price_guard` |
| `src/publisher/governor/eligibility.py` | `unsupported_warehouse_exclusions` |
| `src/publisher/governor/thresholds.py` | `Thresholds` |
| `src/publisher/optimizer/estimator/candidate.py` | `Estimator` |
| `src/publisher/optimizer/estimator/cost.py` | `aws_cluster_hourly_from_evidence`, `resolve_target_sku_price`, `validated_size_rate_ratio` |
| `src/publisher/optimizer/optimizer.py` | `Optimizer` |
| `src/publisher/optimizer/techniques/scaling.py` | `MAX_INCREASE_COST_BOUNDS` |
| `src/publisher/optimizer/techniques/size.py` | `previous_size`, `next_size` |
| `src/publisher/reviewer/package.py` | `validate_locked_package_schema` |
| `src/publisher/reviewer/reviewer.py` | `Reviewer` |
| `src/publisher/packager/adapter.py` | `_scalar_config`, `_ENTITY_QUERY_SOURCE_CATEGORIES`, `_CLIENT_QUERY_SOURCE_CATEGORIES`, `_contract_confidence`, `_contract_range`, `_contract_component_ranges`, `_step_projection`, `_forward_cost_breakdown`, `_actual_cost_breakdown`, `_forecast_confidence`, `_workload_profile`, `_photon_config_observation`, `_technique_id`, `_objectives`, `_recommendation_status`, `_estimation_status`, `_dependency_refs`, `_tunable_knobs`, `_candidate_contracts`, `_compatibility_contract`, `_CONTRACT_EXECUTION_STATE`, `_scenario_estimation_status`, `_scenario_contracts`, `_assessment_contract`, `compute_reevaluation`, `derive_observed_change` |
| `src/publisher/packager/estimator/package.py` | `_economics_point_from_scenario`, `_package_economics` |
| `src/publisher/packager/estimator/scenario.py` | `StatefulBundleEstimator` |
| `src/publisher/packager/estimator/state_transition.py` | `_ChainState` |
| `src/publisher/packager/evidence.py` | `_add_evidence`, `_build_evidence_registry` |
| `src/publisher/packager/execution_plan.py` | `_recommended_plan` |
| `src/publisher/packager/packager.py` | `_build_locked_recommendation_package` |
| `src/publisher/packager/scenarios.py` | `ScenarioPlanner` |
| `src/publisher/packager/selection.py` | `_overall_status` |
| `src/publisher/packager/sequencing.py` | `PLAN_KNOB_RANK`, `_rollback_knobs`, `_rollback_sequencing_note`, `_canary_sequencing_note` |
| `src/publisher/reporter/json_reporter.py` | `_safe_artifact_name`, `package_locked_contract` |
| `src/publisher/publisher.py` | `evaluate_warehouses`, `build_recommendation_packages` |

## After the reorganization

- `scripts/compare_with_ref.py --ref <ref>` diffs the canonical outputs of the working tree against any git ref (including refs from before the package layout); use it before and after a decision-affecting change.
- `tests/regression/test_baseline_compatibility.py` and the golden `expected/` outputs keep pinning behavior; `scripts/update_goldens.py --approved-change "<change>"` rewrites them only with a decision-affecting change.
- The tag `reorg-oracle-2.10.0` remains in git history; nothing reads it.
