"""Isolated external object-store fixture shared by transport and HTTP tests."""

import os
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from evidence_ledger.objects import S3Objects

# 业务 E2E fixture 仓库内的 pytest 用例不得被本仓根收集（故意红测会污染 CI）。
collect_ignore_glob = ["**/fixtures/business_e2e/**"]


@pytest.fixture
def objects():
    endpoint = os.environ.get("RING_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("isolated RING_TEST_S3_ENDPOINT required")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["RING_TEST_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["RING_TEST_S3_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", retries={"max_attempts": 0}),
    )
    bucket = "test-" + uuid4().hex
    client.create_bucket(Bucket=bucket)
    yield S3Objects(client, bucket, max_bytes=1024), client, bucket
    # Test fixture owns this random bucket, never an existing/shared bucket.
    for item in client.list_objects_v2(Bucket=bucket).get("Contents", []):
        client.delete_object(Bucket=bucket, Key=item["Key"])
    client.delete_bucket(Bucket=bucket)
