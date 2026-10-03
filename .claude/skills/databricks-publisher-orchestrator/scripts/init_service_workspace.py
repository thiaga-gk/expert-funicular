#!/usr/bin/env python3
from __future__ import annotations
import argparse
from pathlib import Path

DIRS = ['discovery','sources/collector-sql','catalog','economics','contracts','tests','release']
FILES = {
    'registry.yaml': 'service_id: {service_id}\nartifacts: []\n',
    'discovery/service-profile.yaml': 'version: 1\nstatus: DRAFT\nservice:\n  id: {service_id}\n  resource_noun: resource\n  display_name: TODO\nobjective_hypotheses:\n  primary: TODO\n  constraints_first: true\n',
    'discovery/system-model.yaml': 'version: 1\nstatus: DRAFT\nservice_id: {service_id}\nobjective_hypotheses:\n  primary: TODO\n  secondary: []\nsystem_model:\n  demand_variables: []\n  configurable_state: []\n  capacity_consumption_variables: []\n  pressure_bottleneck_variables: []\n  performance_reliability_outputs: []\n  direct_cost_drivers: []\n  indirect_cost_drivers: []\n  uncontrollable_factors: []\ncausal_edges: []\ncandidate_constraints:\n  hard: []\n  validation_required: []\n  soft: []\nsecond_order_hypotheses: []\n',
    'discovery/open-decisions.yaml': 'version: 1\nstatus: DRAFT\ndecisions: []\n',
    'sources/source-contract.yaml': 'version: 1\nstatus: DRAFT\nresource_identity: []\nsources: []\nfields: []\n',
    'catalog/optimization-policy.yaml': 'version: 1\nstatus: DRAFT\nservice_id: {service_id}\nobjective:\n  mode: LEXICOGRAPHIC\n  primary: TODO\n  secondary: []\nconstraints:\n  hard: []\n  validation_required: []\n  soft: []\n  informational: []\nunknown_evidence_policy:\n  hard_constraint: INSUFFICIENT_EVIDENCE\n  safe_canary_available: TEST_REQUIRED\nscenario_selection:\n  policy: SERVICE_SPECIFIC\n  require_constraint_feasible: true\nrollback_policy:\n  required_for_test_required: true\nrealization_policy:\n  measure_post_change: true\n  preserve_original_package_identity: true\n',
    'catalog/technique-catalog.yaml': 'version: 1\nstatus: DRAFT\ntechniques: []\n',
    'catalog/dependency-graph.yaml': 'version: 1\nstatus: DRAFT\nedges: []\n',
    'economics/economics-contract.yaml': 'version: 1\nstatus: DRAFT\ncurrency_policy: TODO\nobjective_alignment:\n  optimization_policy_ref: catalog/optimization-policy.yaml\n  economics_does_not_override_constraints: true\nactual: {}\nbaseline: {}\ncandidate:\n  is_additive: false\n  counterfactual_must_match_technique_changed_state: true\nscenario:\n  stateful: true\n  applies_second_order_state_transitions: true\n  requires_constraint_feasible_or_testable: true\npackage_envelope: {}\nrealization: {}\nnull_zero_semantics: {}\nsigned_impact: {}\n',
    'contracts/domain-contract.yaml': 'version: 1\nstatus: DRAFT\nresource: {}\ncontracts: [optimization_policy, constraint_spec, constraint_evaluation, causal_trace, validation_plan, second_order_dependencies]\n',
    'contracts/reporter-contract.yaml': 'version: 1\nstatus: DRAFT\ntop_level: [recommendation_package_id, contract_version, resource, current_config, workload_profile, cost_baseline, assessment, evidence, candidates, scenarios, recommended_plan, package_economics, realization, provenance]\ninternal_required_concepts: [optimization_policy, constraint_spec, constraint_evaluation, causal_trace, validation_plan, second_order_dependencies]\n',
    'contracts/workbook-contract.yaml': 'version: 1\nstatus: DRAFT\nsheets: [Executive Summary, Resource Portfolio, Scenario Plans, Recommendation Registry]\n',
    'tests/golden-scenarios.yaml': 'version: 1\nstatus: DRAFT\nscenarios: []\n',
    'tests/acceptance-criteria.yaml': 'version: 1\nstatus: DRAFT\ncriteria: []\n',
    'release/release-evidence.yaml': 'version: 1\nstatus: DRAFT\nframework_coverage: {}\nchecks: []\n',
}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('service_id'); ap.add_argument('--root', default='artifacts/databricks'); ns=ap.parse_args()
    service=ns.service_id.strip().lower().replace('_','-'); out=Path(ns.root)/service
    for d in DIRS: (out/d).mkdir(parents=True, exist_ok=True)
    for rel,text in FILES.items():
        p=out/rel
        if not p.exists(): p.write_text(text.replace('{service_id}',service),encoding='utf-8')
    print(out)
if __name__=='__main__': main()
