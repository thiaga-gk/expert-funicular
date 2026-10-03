#!/usr/bin/env python3
import argparse, sys, yaml
from pathlib import Path
TOP=['recommendation_package_id','contract_version','resource','current_config','workload_profile','cost_baseline','assessment','evidence','candidates','scenarios','recommended_plan','package_economics','realization','provenance']
INTERNAL={'optimization_policy','constraint_spec','constraint_evaluation','causal_trace','validation_plan','second_order_dependencies'}
def load(p): return yaml.safe_load(Path(p).read_text()) or {}
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('contracts_dir'); ns=ap.parse_args(); d=Path(ns.contracts_dir); errs=[]
 for f in ['domain-contract.yaml','reporter-contract.yaml','workbook-contract.yaml']:
  if not (d/f).exists(): errs.append(f'missing {f}')
 if not errs:
  r=load(d/'reporter-contract.yaml'); got=r.get('top_level',[])
  if got!=TOP: errs.append(f'reporter top_level must equal {TOP}')
  concepts=set(r.get('internal_required_concepts') or [])
  missing=INTERNAL-concepts
  if missing: errs.append(f'reporter/domain bundle missing internal framework concepts {sorted(missing)}')
  w=load(d/'workbook-contract.yaml'); sheets=w.get('sheets',[])
  if len(sheets)!=4: errs.append('workbook must declare four default visible sheets unless explicitly versioned otherwise')
 if errs: print('\n'.join(errs)); sys.exit(1)
 print('contract bundle: OK')
if __name__=='__main__': main()
