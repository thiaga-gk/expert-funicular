#!/usr/bin/env python3
import argparse, sys, yaml
from pathlib import Path
REQ={
    'technique_id','title','domain','knob','applicable_resource_types','required_evidence','decision_rule',
    'risk','effort','requires_canary','economics_mechanism','dependencies','invalidates',
    'first_principles','causal_mechanism','constraints','counterfactual','second_order_effects','validation_learning'
}
THINKING_REQ={
    'first_principles': {'problem','driver_changed','objective'},
    'causal_mechanism': {'observation','hypothesis','intervention','expected_direct_effect','falsifiers'},
    'constraints': {'hard','validation_required','soft'},
    'counterfactual': {'model_ref','changed_state','uncertainty'},
    'second_order_effects': {'dependencies','invalidates','may_increase','remeasure_required'},
    'validation_learning': {'requires_canary','success_metrics','rollback_triggers','realization_metrics'},
}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('path'); ns=ap.parse_args()
    d=yaml.safe_load(Path(ns.path).read_text()) or {}; errs=[]; ids=set()
    for i,t in enumerate(d.get('techniques',[])):
        m=REQ-set(t)
        if m: errs.append(f'techniques[{i}] missing {sorted(m)}')
        tid=t.get('technique_id')
        if tid in ids: errs.append(f'duplicate technique_id {tid}')
        ids.add(tid)
        for section, fields in THINKING_REQ.items():
            v=t.get(section) or {}
            missing=fields-set(v)
            if missing: errs.append(f'{tid or i}: {section} missing {sorted(missing)}')
        so=t.get('second_order_effects') or {}
        if list(t.get('dependencies') or []) != list(so.get('dependencies') or []):
            errs.append(f'{tid or i}: top-level dependencies must match second_order_effects.dependencies')
        if list(t.get('invalidates') or []) != list(so.get('invalidates') or []):
            errs.append(f'{tid or i}: top-level invalidates must match second_order_effects.invalidates')
        vl=t.get('validation_learning') or {}
        if bool(t.get('requires_canary')) != bool(vl.get('requires_canary')):
            errs.append(f'{tid or i}: requires_canary must match validation_learning.requires_canary')
    if not ids: errs.append('techniques must not be empty')
    if errs:
        print('\n'.join(errs)); sys.exit(1)
    print('technique catalog: OK')
if __name__=='__main__': main()
