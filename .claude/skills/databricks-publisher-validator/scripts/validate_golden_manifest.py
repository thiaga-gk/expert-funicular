#!/usr/bin/env python3
import argparse, sys, yaml
from pathlib import Path

REQUIRED_KINDS={
    'underutilized','resource_constrained','concurrency_constrained','new_config','sparse_evidence',
    'unsupported_type','pricing_unavailable','dependency_invalidation','migration','healthy_no_change',
    'hard_constraint_satisfied','hard_constraint_violated','hard_constraint_unknown','safe_canary',
    'canary_failure_rollback','causal_falsifier','new_bottleneck_reanalysis'
}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('path'); ns=ap.parse_args()
    d=yaml.safe_load(Path(ns.path).read_text()) or {}
    scenarios=d.get('scenarios',[])
    kinds={x.get('kind') for x in scenarios}
    miss=REQUIRED_KINDS-kinds
    errs=[]
    if miss: errs.append('missing golden scenario kinds: '+', '.join(sorted(miss)))
    for i,s in enumerate(scenarios):
        if not s.get('id'): errs.append(f'scenarios[{i}] missing id')
        if not s.get('kind'): errs.append(f'scenarios[{i}] missing kind')
        if 'expected' not in s: errs.append(f'scenarios[{i}] missing expected')
    if errs:
        print('\n'.join(errs)); sys.exit(1)
    print(f'golden manifest: OK | scenarios={len(scenarios)} framework_cases={len(REQUIRED_KINDS)}')
if __name__=='__main__': main()
