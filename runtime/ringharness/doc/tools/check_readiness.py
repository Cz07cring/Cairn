"""v0.4 specification fixtures only; not a runtime verifier or API test."""
import json
from decimal import Decimal
from pathlib import Path
from content_codec import SCHEMA, validate

ROOT = Path(__file__).resolve().parents[1]

def fixture_verdict(example, run):
    """Evaluate predeclared fixture observations; never establishes evidence trust."""
    verdicts = []
    for criterion in example['criteria']:
        if not criterion['required']:
            continue
        for threshold in example['profile']['thresholds']:
            observations = [o for o in run['observations'] if o['criterion_id'] == criterion['id'] and o['metric'] == threshold['metric']]
            if len(observations) != 1 or observations[0]['status'] != 'OBSERVED' or not observations[0]['evidence_ids']:
                verdicts.append('INSUFFICIENT'); continue
            observation = observations[0]
            metric = next(m for m in example['definition']['metric_definitions'] if m['metric'] == threshold['metric'])
            assert metric['unit'] == threshold['unit']
            actual, expected = observation['value'], threshold['expected']
            if metric['value_type'] in ('INTEGER', 'DECIMAL'):
                actual, expected = Decimal(actual), Decimal(expected)
            if metric['metric'] == 'score_bp':
                assert 0 <= actual <= 10000
            passed = {'EQ': lambda: actual == expected, 'LE': lambda: actual <= expected, 'GE': lambda: actual >= expected}[threshold['operator']]()
            verdicts.append('PASS' if passed else 'FAIL')
    return 'FAIL' if 'FAIL' in verdicts else 'INSUFFICIENT' if not verdicts or 'INSUFFICIENT' in verdicts else 'PASS'

def check():
    fixtures = json.loads((ROOT / 'contracts/readiness-fixtures-v2.json').read_text())
    for case in fixtures['cases']:
        schema = SCHEMA['$defs'][case['definition']]
        for field in case['field_path']:
            schema = schema['properties'][field]
        try:
            validate(case['value'], schema); accepted = True
        except ValueError:
            accepted = False
        assert accepted == case['valid'], case['name']
    examples = json.loads((ROOT / 'contracts/verifier-fixtures-v2.json').read_text())['examples']
    count = 0
    for example in examples:
        validate(example['definition'], SCHEMA['$defs']['VerifierDefinition'])
        validate(example['profile'], SCHEMA['$defs']['VerificationProfile'])
        for case in example['cases']:
            validate(case['run'], SCHEMA['$defs']['VerificationRun'])
            assert fixture_verdict(example, case['run']) == case['expected_verdict'], example['layer']
            count += 1
    return {'status': 'PASS', 'field_event_cases': len(fixtures['cases']), 'verifier_fixture_cases': count, 'runtime_tests': 'NOT_RUN'}

if __name__ == '__main__':
    print(json.dumps(check(), ensure_ascii=False))
