#!/usr/bin/env python3
from __future__ import annotations
import re, sys
from pathlib import Path
try:
    import yaml
except ModuleNotFoundError:
    sys.exit('PyYAML is required: pip install -r requirements.txt')

FORBIDDEN={'README.md','CHANGELOG.md','INSTALLATION_GUIDE.md','QUICK_REFERENCE.md'}
FRAMEWORK=['F1_FIRST_PRINCIPLES','F2_CAUSAL_MECHANISM','F3_CONSTRAINTS_FIRST','F4_COUNTERFACTUAL','F5_SECOND_ORDER_EFFECTS','F6_VALIDATION_LEARNING']

def frontmatter(text:str):
    if not text.startswith('---\n'): raise ValueError('missing YAML frontmatter')
    _, raw, body=text.split('---',2)
    return yaml.safe_load(raw) or {}, body

def main():
    repo=Path(__file__).resolve().parents[1]; root=repo/'.claude/skills'; errs=[]
    skills={p.name for p in root.iterdir() if p.is_dir() and (p/'SKILL.md').exists()}
    manifest_path=root/'suite.yaml'
    if not manifest_path.exists():
        print('ERROR: missing .claude/skills/suite.yaml'); return 1
    manifest=yaml.safe_load(manifest_path.read_text()) or {}
    version=str(manifest.get('version',''))
    if not re.fullmatch(r'\d+\.\d+\.\d+', version): errs.append(f'suite version must be semver, got {version!r}')
    if manifest.get('entrypoint')!='databricks-publisher-orchestrator': errs.append('suite entrypoint mismatch')
    expected={x.get('name') for x in manifest.get('skills',[])}
    if skills!=expected: errs.append(f'skill set mismatch: manifest={sorted(expected)} on disk={sorted(skills)}')
    fw=(manifest.get('optimization_framework') or {}).get('axes') or []
    if fw!=FRAMEWORK: errs.append(f'optimization framework axes must equal {FRAMEWORK}')
    framework_ref=root/'databricks-publisher-orchestrator/references/optimization-thinking-framework.md'
    if not framework_ref.exists(): errs.append('missing optimization-thinking-framework.md')
    total_triggers=0; total_functional=0
    for name in sorted(skills):
        b=root/name; text=(b/'SKILL.md').read_text(encoding='utf-8')
        try: fm, body=frontmatter(text)
        except Exception as e: errs.append(f'{name}: {e}'); continue
        if fm.get('name')!=name: errs.append(f'{name}: frontmatter name mismatch')
        desc=str(fm.get('description','')).strip()
        if len(desc)<120: errs.append(f'{name}: description too weak/short ({len(desc)})')
        if len(body.splitlines())>500: errs.append(f'{name}: SKILL.md body exceeds 500 lines')
        if 'optimization-thinking-framework.md' not in body and name!='databricks-publisher-orchestrator':
            errs.append(f'{name}: must reference the optimization thinking framework')
        if re.search(r'(?m)^\s*TODO(?:[:\s]|$)', text): errs.append(f'{name}: unresolved TODO placeholder in SKILL.md')
        for bad in FORBIDDEN:
            if (b/bad).exists(): errs.append(f'{name}: forbidden auxiliary file {bad}')
        trig=yaml.safe_load((b/'evals/triggers.yaml').read_text()) or {}; pos=trig.get('positive',[]); neg=trig.get('negative',[])
        total_triggers += len(pos)+len(neg)
        if len(pos)!=10 or len(neg)!=10: errs.append(f'{name}: expected 10 positive + 10 negative trigger cases')
        fun=yaml.safe_load((b/'evals/functional.yaml').read_text()) or {}; cases=fun.get('cases',[]); total_functional += len(cases)
        if len(cases)<3: errs.append(f'{name}: requires >=3 functional evals')
        for ref in re.findall(r'`(references/[^`]+)`', body):
            if not (b/ref).exists() and 'sibling orchestrator' not in body[max(0,body.find(ref)-100):body.find(ref)+100]:
                errs.append(f'{name}: missing referenced file {ref}')
        for py in (b/'scripts').rglob('*.py'):
            try: compile(py.read_text(encoding='utf-8'), str(py), 'exec')
            except Exception as e: errs.append(f'{name}: script compile failed {py.name}: {e}')
    tech=root/'databricks-technique-catalog'
    for req in ['assets/optimization-policy.template.yaml','assets/technique.template.yaml','scripts/validate_optimization_policy.py','scripts/validate_technique_catalog.py']:
        if not (tech/req).exists(): errs.append(f'technique catalog missing {req}')
    disc=root/'databricks-service-discovery/assets/system-model.template.yaml'
    if not disc.exists(): errs.append('discovery missing system-model.template.yaml')
    cross=yaml.safe_load((root/'databricks-publisher-orchestrator/evals/cross-service.yaml').read_text()) or {}
    if {c.get('id') for c in cross.get('cases',[])} != {'sql-warehouse-control','job-cluster-adaptation','lakeflow-anti-copy'}:
        errs.append('cross-service eval set incomplete')
    if errs:
        print('\n'.join('ERROR: '+e for e in errs)); return 1
    print(f'skill suite: OK | version={version} skills={len(skills)} trigger_cases={total_triggers} functional_cases={total_functional} cross_service_cases={len(cross.get("cases",[]))} framework_axes={len(FRAMEWORK)}')
    return 0
if __name__=='__main__': sys.exit(main())
