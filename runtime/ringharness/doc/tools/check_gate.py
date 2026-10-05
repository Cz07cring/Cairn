"""Executable v0.5 specification examples; no runtime, database or concurrency proof."""
import json
from pathlib import Path

def aggregate(case):
    effective = [v['verdict'] for v in case['assessments'] if v['valid']]
    if 'FAIL' in effective or 'FAIL' in case['audits']:
        return 'FAIL'
    if case['trust'] != 'OPEN' or case['pending'] or not case['audits'] or any(v != 'PASS' for v in case['audits']):
        return 'INSUFFICIENT'
    return 'PASS' if 'PASS' in effective else 'INSUFFICIENT'

def check():
    source=Path(__file__).resolve().parents[1]/'contracts/gate-fixtures-v3.json'
    data=json.loads(source.read_text())
    for case in data['aggregation']:
        assert aggregate(case)==case['expected'],case['name']
    for case in data['admission']:
        allowed=case['trust']=='OPEN' and case['skill']=='ACTIVE'
        assert allowed==case['expected'],case['name']
    for case in data['unlock']:
        allowed=(case['expected_revision']==case['current_revision'] and case['propagation_complete'] and case['affected_writes_drained'])
        assert allowed==case['expected'],case['name']
    return {'status':'PASS','specification_cases':sum(len(data[k]) for k in ('aggregation','admission','unlock')),'runtime_tests':'NOT_RUN'}

if __name__=='__main__':
    print(json.dumps(check()))
