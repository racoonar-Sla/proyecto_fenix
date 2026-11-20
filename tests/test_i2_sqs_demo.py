"""Pruebas unitarias del Integrante 2: SQS (colas, DLQ, backoff) y Demo (EC2/S3)."""
import json
from unittest.mock import patch
import boto3
import pytest
from moto import mock_aws

from fenix.services import demo, sqs
from fenix.utils.errors import InventoryError
from fenix.utils.state import load


@pytest.fixture
def moto_env(monkeypatch, tmp_path):
    """Aísla las credenciales y apunta Boto3 a Moto en memoria."""
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("GROUP_ID", "grupo01")
    monkeypatch.setenv("PROJECT", "fenix")

    with mock_aws():
        clients = {
            "sqs": boto3.client("sqs", region_name="us-east-1"),
            "ec2": boto3.client("ec2", region_name="us-east-1"),
            "s3": boto3.client("s3", region_name="us-east-1"),
        }

        def fake_client(service: str):
            return clients[service]

        with patch("fenix.services.sqs.client", side_effect=fake_client), \
             patch("fenix.services.demo.client", side_effect=fake_client):
            yield clients, tmp_path


def test_ensure_queues_creates_dlq_and_redrive_idempotently(moto_env):
    clients, _ = moto_env
    work_url, dlq_url = sqs.ensure_queues("fenix-grupo01")

    assert work_url.endswith("fenix-grupo01-events")
    assert dlq_url.endswith("fenix-grupo01-dlq")

    attrs = clients["sqs"].get_queue_attributes(
        QueueUrl=work_url,
        AttributeNames=["VisibilityTimeout", "MessageRetentionPeriod", "RedrivePolicy"],
    )["Attributes"]

    assert attrs["VisibilityTimeout"] == "45"
    assert attrs["MessageRetentionPeriod"] == "345600"
    redrive = json.loads(attrs["RedrivePolicy"])
    assert int(redrive["maxReceiveCount"]) == 3
    assert "fenix-grupo01-dlq" in redrive["deadLetterTargetArn"]

    # Repetir ensure_queues debe devolver exactamente las mismas URLs (idempotencia)
    work_url_2, dlq_url_2 = sqs.ensure_queues("fenix-grupo01")
    assert (work_url_2, dlq_url_2) == (work_url, dlq_url)


def test_retry_later_applies_capped_exponential_backoff(moto_env):
    work_url, _ = sqs.ensure_queues("fenix-grupo01")
    sqs.publish(work_url, {"event_id": "evt-backoff-01", "event_type": "instance.stopped"})
    messages = sqs.receive(work_url)
    assert len(messages) == 1
    msg = messages[0]

    assert sqs.retry_later(work_url, {**msg, "Attributes": {"ApproximateReceiveCount": "1"}}) == 2
    assert sqs.retry_later(work_url, {**msg, "Attributes": {"ApproximateReceiveCount": "2"}}) == 4
    assert sqs.retry_later(work_url, {**msg, "Attributes": {"ApproximateReceiveCount": "3"}}) == 8
    assert sqs.retry_later(work_url, {**msg, "Attributes": {"ApproximateReceiveCount": "5"}}) == 30


def test_setup_demo_and_fault_workflow(moto_env):
    clients, tmp_path = moto_env
    inv_file = str(tmp_path / "inventory.json")

    inv1 = demo.setup_demo(inventory_path=inv_file)
    assert inv1["node-01"]["type"] == "ec2"
    assert inv1["bucket-01"]["type"] == "s3"
    assert load(inv_file) == inv1

    # Idempotencia: segunda llamada conserva la misma instancia EC2 y bucket
    inv2 = demo.setup_demo(inventory_path=inv_file)
    assert inv2["node-01"]["instance_id"] == inv1["node-01"]["instance_id"]
    assert inv2["bucket-01"]["bucket_name"] == inv1["bucket-01"]["bucket_name"]

    # Falla EC2: debe detener la instancia
    demo.fault("node-01", inventory_path=inv_file)
    desc = clients["ec2"].describe_instances(InstanceIds=[inv1["node-01"]["instance_id"]])
    state = desc["Reservations"][0]["Instances"][0]["State"]["Name"]
    assert state in ("stopping", "stopped")

    # Falla S3: debe eliminar los tags del bucket
    demo.fault("bucket-01", inventory_path=inv_file)
    with pytest.raises(Exception):
        clients["s3"].get_bucket_tagging(Bucket=inv1["bucket-01"]["bucket_name"])

    # Recurso inexistente: debe lanzar InventoryError
    with pytest.raises(InventoryError):
        demo.fault("nodo-inexistente", inventory_path=inv_file)
