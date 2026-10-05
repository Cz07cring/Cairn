"""The approved registry is an explicit test precondition, not a tested deployment loader."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from evidence_ledger.content import encode
from sqlalchemy import create_engine, text


def test_profiles_bind_only_to_matching_approved_verifier(api):
    client, token = api
    auth = {"Authorization": "Bearer " + token(str(uuid4()), ["admin", "viewer"])}
    project = client.post(
        "/api/v1/projects",
        json={"name": "verifiers", "repository_ref": "fixture"},
        headers={**auth, "Idempotency-Key": str(uuid4())},
    ).json()["data"]["id"]
    example = json.loads(
        (Path(__file__).parents[2] / "doc/contracts/verifier-fixtures-v2.json").read_text()
    )["examples"][0]
    definition = {**example["definition"], "project_id": project}
    digest = (
        "sha256:"
        + hashlib.sha256(
            encode(
                json.dumps(
                    {
                        "schema_version": 3,
                        "object_type": "VerifierDefinition",
                        "content": definition,
                        "reference_bindings": [],
                    }
                )
            )
        ).hexdigest()
    )
    profile = {
        **example["profile"],
        "project_id": project,
        "verifier_ref": "approved.test",
        "verifier_digest": digest,
    }

    def submit(body):
        return client.post(
            "/api/v1/verification-profiles",
            json=body,
            headers={**auth, "Idempotency-Key": str(uuid4())},
        )

    # Unknown definitions cannot be activated by merely naming an entrypoint in a profile.
    assert submit(profile).status_code == 422
    engine = create_engine(os.environ["RING_TEST_DATABASE_URL"])
    with engine.begin() as db:
        db.execute(
            text("""INSERT INTO verifier_definitions(project_id,ref,content_digest,content,approval_record_digest)
            VALUES(:project,'approved.test',:digest,CAST(:content AS jsonb),:approval)"""),
            {
                "project": project,
                "digest": digest,
                "content": json.dumps(definition),
                "approval": "sha256:" + "b" * 64,
            },
        )
    engine.dispose()
    result = submit(profile)
    assert result.status_code == 201, result.text
    assert result.json()["data"]["config"]["verifier_digest"] == digest
    assert client.get(
        "/api/v1/verification-profiles", params={"project_id": project}, headers=auth
    ).json()["data"] == [result.json()["data"]]
    for change in [
        {"verifier_digest": "sha256:" + "c" * 64},
        {"required_layers": ["SEMANTIC"]},
        {
            "thresholds": [
                {"metric": "checks_passed", "unit": "wrong", "operator": "EQ", "expected": "true"}
            ]
        },
        {
            "thresholds": [
                {"metric": "checks_passed", "unit": "boolean", "operator": "GE", "expected": "true"}
            ]
        },
    ]:
        assert submit({**profile, **change}).status_code == 422
