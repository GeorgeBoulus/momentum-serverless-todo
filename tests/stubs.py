"""Minimal stand-ins for boto3/botocore so the Lambda modules import and run offline (stdlib only)."""
import os
import sys
import types

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Any:
    def __getattr__(self, name):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()


def install():
    sys.path.insert(0, os.path.abspath(SRC))
    bc, bce = types.ModuleType("botocore"), types.ModuleType("botocore.exceptions")
    bce.ClientError = ClientError
    b3 = types.ModuleType("boto3")
    b3.client = lambda *a, **k: _Any()
    b3.resource = lambda *a, **k: _Any()
    sys.modules.update({"botocore": bc, "botocore.exceptions": bce, "boto3": b3})
    for name, attrs in [("boto3.dynamodb", {}), ("boto3.dynamodb.conditions", {"Key": _Any}),
                        ("boto3.dynamodb.types", {"TypeDeserializer": _Any})]:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
    for k in ("TASKS_TABLE", "SCORES_TABLE", "HASHES_TABLE", "PROOF_BUCKET"):
        os.environ.setdefault(k, "test")


install()
