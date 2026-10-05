"""Frozen, independently specified byte vectors exercise the production codec boundary."""

import hashlib
import json
from pathlib import Path

import pytest
from evidence_ledger.content import encode

VECTORS = json.loads((Path(__file__).parents[2] / "doc/contracts/hash-vectors-v3.json").read_text())


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["name"])
def test_canonical_bytes_and_digest_match_frozen_vectors(case):
    result = encode(case["input_json"])
    assert result.decode() == case["canonical_utf8"]
    assert hashlib.sha256(result).hexdigest() == case["sha256"]


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda c: c["name"])
def test_invalid_content_is_rejected(case):
    with pytest.raises(ValueError):
        encode(case["input_json"])
