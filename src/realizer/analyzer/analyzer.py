"""Analyzer facade: what actually happened in this run's observation, and the history it follows (its own ledger and the
other lineages of its warehouse).

The Analyzer observes; it neither decides what the policy permits (Governor) nor relates the observation to the plan.
"""
from __future__ import annotations

from realizer.analyzer import analysis
from realizer.analyzer.lineages import read_warehouse_history
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.request import RunContext
from realizer.packager.ledger import ledger_path, load_ledger


def run(context: RunContext) -> RealizationAnalysis:
    """Normalize the observation (``None`` for a lineage-only run), read the ledger the run starts from and the rest of
    the warehouse's history in the output directory (its other lineages and the recorded policy snapshots).

    Everything is only read. A malformed observation raises here, as it raised at the start of the engine's
    observation processing, and so does a ``rollback_observed`` flag its configuration contradicts (the first check that
    needs the history); an edited source package copy or a corrupt ledger of another lineage raises after it. Nothing is
    written by this capability.
    """
    request = context.request
    observation = analysis.analyze_observation(request.package, request.observation) if request.observation else None
    history = analysis.summarize_history(load_ledger(ledger_path(request.output_dir, context.realization_package_id)))
    if observation is not None:
        analysis.check_rollback_flag(request.package, history, observation)
    drift = observation is not None and analysis.config_drift(history, observation.observed_config_hash, observation.observed_at_utc)
    warehouse = read_warehouse_history(request.output_dir, request.package)
    return RealizationAnalysis(observation=observation, history=history, config_drift_observed=drift, warehouse=warehouse)
