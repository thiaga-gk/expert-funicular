#!/usr/bin/env python3
import argparse, sys, yaml
from pathlib import Path
REQ=['currency_policy','objective_alignment','actual','baseline','candidate','scenario','package_envelope','realization','null_zero_semantics','signed_impact']
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('path'); ns=ap.parse_args(); d=yaml.safe_load(Path(ns.path).read_text()) or {}; errs=[f'missing {k}' for k in REQ if k not in d]
 oa=d.get('objective_alignment') or {}
 if oa.get('economics_does_not_override_constraints') is not True: errs.append('objective_alignment.economics_does_not_override_constraints must be true')
 cand=d.get('candidate') or {}
 if cand.get('is_additive') is not False: errs.append('candidate.is_additive must be false')
 if cand.get('counterfactual_must_match_technique_changed_state') is not True: errs.append('candidate counterfactual must match technique changed state')
 sc=d.get('scenario') or {}
 if sc.get('stateful') is not True: errs.append('scenario.stateful must be true')
 if sc.get('applies_second_order_state_transitions') is not True: errs.append('scenario.applies_second_order_state_transitions must be true')
 if sc.get('requires_constraint_feasible_or_testable') is not True: errs.append('scenario.requires_constraint_feasible_or_testable must be true')
 if errs: print('\n'.join(errs)); sys.exit(1)
 print('economics contract: OK')
if __name__=='__main__': main()
