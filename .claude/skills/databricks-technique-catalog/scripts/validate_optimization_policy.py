#!/usr/bin/env python3
import argparse, sys, yaml
from pathlib import Path
REQ={'version','status','service_id','objective','constraints','unknown_evidence_policy','scenario_selection','rollback_policy','realization_policy'}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('path'); ns=ap.parse_args()
    d=yaml.safe_load(Path(ns.path).read_text()) or {}; errs=[]
    missing=REQ-set(d)
    if missing: errs.append(f'missing {sorted(missing)}')
    obj=d.get('objective') or {}
    if obj.get('mode')!='LEXICOGRAPHIC': errs.append('objective.mode must be LEXICOGRAPHIC')
    if not obj.get('primary'): errs.append('objective.primary is required')
    cons=d.get('constraints') or {}
    for k in ('hard','validation_required','soft','informational'):
        if k not in cons: errs.append(f'constraints.{k} is required')
    sel=d.get('scenario_selection') or {}
    if sel.get('require_constraint_feasible') is not True: errs.append('scenario_selection.require_constraint_feasible must be true')
    if errs:
        print('\n'.join(errs)); return 1
    print('optimization policy: OK'); return 0
if __name__=='__main__': sys.exit(main())
