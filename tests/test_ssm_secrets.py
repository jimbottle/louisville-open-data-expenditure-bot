"""app._load_secrets_from_ssm: the cold-start secret load for Lambda.

Secrets are SecureStrings under SSM_PARAMETER_PATH and must land in os.environ
before ADMIN_TOKEN and the LLM clients read it; an explicit env var wins; the
values are never logged; without the variable the loader is a no-op.
"""
import logging
import os

import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def loader(monkeypatch):
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    import app
    return app._load_secrets_from_ssm


def test_noop_without_path(loader, monkeypatch):
    monkeypatch.delenv("SSM_PARAMETER_PATH", raising=False)
    assert loader() == 0


def test_loads_secure_strings_into_env_without_logging_values(loader, monkeypatch, caplog):
    with mock_aws():
        ssm = boto3.client("ssm", region_name="us-east-1")
        for name, value in (("OPENROUTER_API_KEY", "or-secret-1"), ("ADMIN_TOKEN", "adm-secret-2")):
            ssm.put_parameter(Name=f"/lou/test/{name}", Value=value, Type="SecureString")
        ssm.put_parameter(Name="/lou/other/NOT_MINE", Value="x", Type="String")
        monkeypatch.setenv("SSM_PARAMETER_PATH", "/lou/test/")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("NOT_MINE", raising=False)
        with caplog.at_level(logging.INFO):
            assert loader() == 2
        assert os.environ["OPENROUTER_API_KEY"] == "or-secret-1"
        assert os.environ["ADMIN_TOKEN"] == "adm-secret-2"
        assert "NOT_MINE" not in os.environ
        assert "or-secret-1" not in caplog.text and "adm-secret-2" not in caplog.text
        assert "Loaded 2 secret(s)" in caplog.text
        # The third expected name was absent: reported by NAME, values never.
        assert "not found under /lou/test: CEREBRAS_PAID_API_KEY" in caplog.text


def test_explicit_env_wins_over_the_parameter(loader, monkeypatch):
    with mock_aws():
        boto3.client("ssm", region_name="us-east-1").put_parameter(
            Name="/lou/test/ADMIN_TOKEN", Value="from-ssm", Type="SecureString")
        monkeypatch.setenv("SSM_PARAMETER_PATH", "/lou/test")
        monkeypatch.setenv("ADMIN_TOKEN", "from-env")
        assert loader() == 0
        assert os.environ["ADMIN_TOKEN"] == "from-env"
