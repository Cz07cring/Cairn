"""Frozen Content v3 checker, restricted JSON Schema subset; not a production validator."""
import json
import re
from pathlib import Path

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / 'contracts/content-v3.schema.json').read_text())


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _integer(value):
    if value == '-0':
        raise ValueError('negative zero')
    n = int(value)
    if abs(n) > 9007199254740991:
        raise ValueError('unsafe integer')
    return n


def _invalid(value):
    raise ValueError('non-integer number: ' + value)


def parse(raw):
    value = json.loads(raw, object_pairs_hook=_pairs, parse_int=_integer,
                       parse_float=_invalid, parse_constant=_invalid)
    def visit(v):
        if isinstance(v, str):
            v.encode('utf-8', errors='strict')
        elif isinstance(v, list):
            for item in v: visit(item)
        elif isinstance(v, dict):
            for k, item in v.items(): visit(k); visit(item)
    visit(value)
    return value


def _resolve(s):
    return SCHEMA['$defs'][s['$ref'].split('/')[-1]] if '$ref' in s else s


def validate(v, s):
    s = _resolve(s)
    for branch in s.get('allOf', []): validate(v, branch)
    if 'if' in s:
        try: validate(v, s['if']); condition = True
        except ValueError: condition = False
        validate(v, s.get('then' if condition else 'else', {}))
    if 'const' in s:
        if type(v) is not type(s['const']) or v != s['const']:
            raise ValueError('const mismatch')
    for union in ('oneOf', 'anyOf'):
        if union in s:
            matches = 0
            for branch in s[union]:
                try: validate(v, branch); matches += 1
                except ValueError: pass
            if not matches or (union == 'oneOf' and matches != 1):
                raise ValueError('union mismatch')
            return
    t = s.get('type')
    if t == 'null' and v is not None: raise ValueError('not null')
    if t == 'boolean' and type(v) is not bool: raise ValueError('not bool')
    if t == 'integer':
        if type(v) is not int or not s.get('minimum', -9007199254740991) <= v <= s.get('maximum', 9007199254740991):
            raise ValueError('integer bounds')
    if t == 'string':
        if not isinstance(v, str): raise ValueError('not string')
        if not s.get('minLength', 0) <= len(v) <= s.get('maxLength', 10000000): raise ValueError('string length')
        if 'pattern' in s and not re.search(s['pattern'], v): raise ValueError('string pattern')
    if 'enum' in s and v not in s['enum']: raise ValueError('enum mismatch')
    if t == 'array':
        if not isinstance(v, list) or not s.get('minItems', 0) <= len(v) <= s.get('maxItems', 10000000): raise ValueError('array shape')
        for item in v: validate(item, s.get('items', {}))
        if 'contains' in s:
            matches = 0
            for item in v:
                try: validate(item, s['contains']); matches += 1
                except ValueError: pass
            if matches < s.get('minContains', 1): raise ValueError('contains mismatch')
    if t == 'object' or ('properties' in s and isinstance(v, dict)):
        if not isinstance(v, dict): raise ValueError('not object')
        if any(k not in v for k in s.get('required', [])): raise ValueError('required field')
        properties = s.get('properties', {})
        for k, item in v.items():
            if k in properties: validate(item, properties[k])
            elif s.get('additionalProperties') is False: raise ValueError('unknown field')
            elif isinstance(s.get('additionalProperties'), dict): validate(item, s['additionalProperties'])


def canonical(v):
    if isinstance(v, dict):
        return '{' + ','.join(canonical(k) + ':' + canonical(v[k]) for k in sorted(v, key=lambda k: k.encode('utf-16-be'))) + '}'
    if isinstance(v, list): return '[' + ','.join(canonical(x) for x in v) + ']'
    return json.dumps(v, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def normalize(v, s):
    s = _resolve(s)
    for union in ('oneOf', 'anyOf'):
        if union in s:
            for branch in s[union]:
                try: validate(v, branch)
                except ValueError: continue
                return normalize(v, branch)
    if s.get('type') == 'object':
        return {k: normalize(x, s.get('properties', {}).get(k, s.get('additionalProperties', {}))) for k, x in v.items()}
    if s.get('type') == 'array':
        values = [normalize(x, s['items']) for x in v]
        if s.get('x-order') == 'set':
            keys = s.get('x-sort-keys')
            def key(x):
                return tuple(str(x[k]).encode('utf-16-be') for k in keys) if keys else canonical(x).encode('utf-8')
            keyed = [(key(x), x) for x in values]
            if len({k for k, _ in keyed}) != len(keyed): raise ValueError('duplicate set key')
            values = [x for _, x in sorted(keyed, key=lambda pair: pair[0])]
        return values
    return v


def encode(raw):
    value = parse(raw)
    validate(value, SCHEMA)
    return canonical(normalize(value, SCHEMA)).encode('utf-8')
