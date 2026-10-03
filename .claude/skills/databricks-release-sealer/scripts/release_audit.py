#!/usr/bin/env python3
from __future__ import annotations
import argparse, re, sys
from pathlib import Path
DEFAULT_BAD=[r'workbook deferred',r'legacy_report',r'_legacy_package',r'SimpleXlsxWriter',r'TODO: remove',r'DATABRICKS_TOKEN\s*=\s*[A-Za-z0-9]']
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('repo'); ap.add_argument('--allow',action='append',default=[]); ns=ap.parse_args(); root=Path(ns.repo); errs=[]
 pats=[re.compile(x,re.I) for x in DEFAULT_BAD]
 for p in root.rglob('*'):
  if not p.is_file() or any(x in p.parts for x in ['.git','__pycache__']): continue
  if 'evals' in p.parts or p.name=='release_audit.py': continue
  if p.suffix.lower() not in {'.py','.md','.yaml','.yml','.json','.toml','.txt'}: continue
  try: text=p.read_text(errors='ignore')
  except Exception: continue
  for rx in pats:
   if rx.search(text) and not any(a in str(p) for a in ns.allow): errs.append(f'{p}: matched {rx.pattern}')
 if list(root.rglob('__pycache__')): errs.append('__pycache__ directories present')
 if errs: print('\n'.join(errs)); sys.exit(1)
 print('release audit: OK')
if __name__=='__main__': main()
