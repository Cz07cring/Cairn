"""Real S3-compatible transport tests. No mocks or fabricated integrity verdicts."""

from io import BytesIO
from uuid import uuid4

import pytest


def test_content_addressed_roundtrip_and_idempotent_write(objects):
    store, _, _ = objects
    project = uuid4()
    digest = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    receipt = store.put(project, digest, BytesIO(b"abc"))
    assert receipt.digest == digest
    assert receipt.size_bytes == 3
    assert store.read(project, digest) == b"abc"
    assert store.put(project, digest, BytesIO(b"abc")) == receipt


def test_objects_from_another_project_are_not_readable(objects):
    store, _, _ = objects
    digest = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    store.put(uuid4(), digest, BytesIO(b"abc"))
    with pytest.raises(FileNotFoundError):
        store.read(uuid4(), digest)


def test_wrong_digest_and_oversized_upload_never_publish(objects):
    from evidence_ledger.objects import IntegrityError, ObjectTooLarge

    store, client, bucket = objects
    project = uuid4()
    digest = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    with pytest.raises(IntegrityError):
        store.put(project, digest, BytesIO(b"wrong"))
    with pytest.raises(ObjectTooLarge):
        store.put(project, digest, BytesIO(b"x" * 1025))
    assert client.list_objects_v2(Bucket=bucket)["KeyCount"] == 0


def test_storage_corruption_is_rejected_on_read_and_duplicate_put(objects):
    from evidence_ledger.objects import IntegrityError

    store, client, bucket = objects
    project = uuid4()
    digest = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    store.put(project, digest, BytesIO(b"abc"))
    # Fault injection through the privileged test storage account, not the application API.
    key = client.list_objects_v2(Bucket=bucket)["Contents"][0]["Key"]
    client.put_object(Bucket=bucket, Key=key, Body=b"bad")
    with pytest.raises(IntegrityError):
        store.read(project, digest)
    with pytest.raises(IntegrityError):
        store.put(project, digest, BytesIO(b"abc"))
    assert client.get_object(Bucket=bucket, Key=key)["Body"].read() == b"bad"


def test_invalid_addresses_are_rejected_before_storage_access(objects):
    store, _, _ = objects
    with pytest.raises(ValueError):
        store.put(uuid4(), "sha1:" + "a" * 40, BytesIO(b"abc"))
    with pytest.raises(ValueError):
        store.read(uuid4(), "../../other-project")
