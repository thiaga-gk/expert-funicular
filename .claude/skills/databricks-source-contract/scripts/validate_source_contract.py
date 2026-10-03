#!/usr/bin/env python3
import argparse, sys, yaml
from pathlib import Path
REQUIRED={'field','semantic','source','source_field','classification','grain','required','nullable','scope','freshness','retention','epoch_role','correction_semantics','fallback','consumers','framework_roles'}
CLASSES={'AUTHORITATIVE','OBSERVED','DERIVED','COMPATIBILITY_ONLY','UNAVAILABLE'}
ROLES={'IDENTITY','REGIME','CAUSAL_SIGNAL','CONSTRAINT','COUNTERFACTUAL','ECONOMICS','VALIDATION','ATTRIBUTION','COMPATIBILITY'}
def main():
 p=argparse.ArgumentParser(); p.add_argument('path'); ns=p.parse_args(); data=yaml.safe_load(Path(ns.path).read_text()) or {}
 errs=[]
 if not data.get('resource_identity'): errs.append('resource_identity is required')
 for i,f in enumerate(data.get('fields',[])):
  miss=REQUIRED-set(f)
  if miss: errs.append(f'fields[{i}] missing {sorted(miss)}')
  if f.get('classification') not in CLASSES: errs.append(f'fields[{i}] invalid classification')
  roles=set(f.get('framework_roles') or [])
  bad=roles-ROLES
  if bad: errs.append(f'fields[{i}] invalid framework_roles {sorted(bad)}')
 if not data.get('fields'): errs.append('fields must not be empty')
 if errs:
  print('\n'.join(errs)); sys.exit(1)
 print('source contract: OK')
if __name__=='__main__': main()
