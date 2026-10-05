"""Content v3 canonical encoding. Integrity only; provenance and acceptance are separate."""

import json
from importlib.resources import files

from jsonschema import Draft202012Validator, ValidationError

SCHEMA = json.loads(files(__package__).joinpath("content-v3.schema.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
_VALIDATOR = Draft202012Validator(SCHEMA)


def validate(value, schema):
    try:
        _VALIDATOR.evolve(schema=schema).validate(value)
    except ValidationError as exc:
        raise ValueError("invalid Content schema") from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _integer(value):
    if value == "-0":
        raise ValueError("negative zero")
    n = int(value)
    if abs(n) > 9007199254740991:
        raise ValueError("unsafe integer")
    return n


def _invalid(value):
    raise ValueError("non-integer number: " + value)


def parse(raw):
    value = json.loads(
        raw,
        object_pairs_hook=_pairs,
        parse_int=_integer,
        parse_float=_invalid,
        parse_constant=_invalid,
    )

    def visit(v):
        if isinstance(v, str):
            v.encode("utf-8", errors="strict")
        elif isinstance(v, list):
            for item in v:
                visit(item)
        elif isinstance(v, dict):
            for k, item in v.items():
                visit(k)
                visit(item)

    visit(value)
    return value


def _resolve(s):
    return SCHEMA["$defs"][s["$ref"].split("/")[-1]] if "$ref" in s else s


def canonical(v):
    if isinstance(v, dict):
        return (
            "{"
            + ",".join(
                canonical(k) + ":" + canonical(v[k])
                for k in sorted(v, key=lambda k: k.encode("utf-16-be"))
            )
            + "}"
        )
    if isinstance(v, list):
        return "[" + ",".join(canonical(x) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def normalize(v, s):
    s = _resolve(s)
    for union in ("oneOf", "anyOf"):
        if union in s:
            for branch in s[union]:
                try:
                    validate(v, branch)
                except ValueError:
                    continue
                return normalize(v, branch)
    if s.get("type") == "object":
        return {
            k: normalize(x, s.get("properties", {}).get(k, s.get("additionalProperties", {})))
            for k, x in v.items()
        }
    if s.get("type") == "array":
        values = [normalize(x, s["items"]) for x in v]
        if s.get("x-order") == "set":
            keys = s.get("x-sort-keys")

            def key(x):
                return (
                    tuple(str(x[k]).encode("utf-16-be") for k in keys)
                    if keys
                    else canonical(x).encode("utf-8")
                )

            keyed = [(key(x), x) for x in values]
            if len({k for k, _ in keyed}) != len(keyed):
                raise ValueError("duplicate set key")
            values = [x for _, x in sorted(keyed, key=lambda pair: pair[0])]
        return values
    return v


def encode(raw: str) -> bytes:
    """Reject ambiguous JSON and encode validated Content with frozen set semantics."""
    if len(raw.encode("utf-8")) > 1024 * 1024:
        raise ValueError("Content exceeds 1 MiB")
    try:
        value = parse(raw)
        validate(value, SCHEMA)
        return canonical(normalize(value, SCHEMA)).encode("utf-8")
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("invalid JSON depth or Unicode") from exc
