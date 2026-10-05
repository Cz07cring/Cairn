"""Public reads of a trusted fixture; this does not validate collector authorization."""

from io import BytesIO
from uuid import UUID, uuid4

from control_kernel.storage.artifacts import Artifacts


def test_artifact_metadata_and_content_require_project_membership(api, objects):
    client, token = api
    object_store, _, _ = objects
    subject = str(uuid4())
    headers = {
        "Authorization": "Bearer " + token(subject, ["admin", "viewer"]),
        "Idempotency-Key": str(uuid4()),
    }
    project = client.post(
        "/api/v1/projects", json={"name": "证据", "repository_ref": "fixture"}, headers=headers
    ).json()["data"]["id"]
    # Trusted collector boundary is a fixture until Activity/fencing admission exists.
    client.app.state.objects = object_store
    record = Artifacts(client.app.state.engine, object_store).ingest_raw(
        UUID(project),
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        BytesIO(b"abc"),
        mime="text/html",
        producer_identity="fixture:collector",
    )
    path = f"/api/v1/artifacts/{record.id}"
    assert client.get(path).status_code == 401
    metadata = client.get(path, headers=headers)
    assert metadata.status_code == 200
    assert metadata.json()["data"]["size_bytes"] == 3
    assert "storage_key" not in metadata.json()["data"]
    content = client.get(path + "/content", headers=headers)
    assert content.status_code == 200 and content.content == b"abc"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["content-disposition"].startswith("attachment")
    assert content.headers["etag"] == '"' + record.digest + '"'
    other = {"Authorization": "Bearer " + token(str(uuid4()), ["admin", "viewer"])}
    assert client.get(path, headers=other).status_code == 404
    assert client.get(path + "/content", headers=other).status_code == 404
    partial = client.get(path + "/content", headers={**headers, "Range": "bytes=1-2"})
    assert partial.status_code == 206 and partial.content == b"bc"
    assert partial.headers["content-range"] == "bytes 1-2/3"
    suffix = client.get(path + "/content", headers={**headers, "Range": "bytes=-1"})
    assert suffix.status_code == 206 and suffix.content == b"c"
    assert (
        client.get(path + "/content", headers={**headers, "Range": "bytes=3-"}).status_code == 416
    )
    # Full verification must run even when only one byte is requested.
    _, storage_client, bucket = objects
    key = storage_client.list_objects_v2(Bucket=bucket)["Contents"][0]["Key"]
    storage_client.put_object(Bucket=bucket, Key=key, Body=b"abd")
    broken = client.get(path + "/content", headers={**headers, "Range": "bytes=0-0"})
    assert broken.status_code == 422
    assert broken.json()["error"]["code"] == "EVIDENCE_INVALID"
