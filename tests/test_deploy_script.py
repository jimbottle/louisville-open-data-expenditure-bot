"""infra/cdk/deploy.sh against stubbed aws/npx/curl/dig binaries (hermetic: no
call leaves the machine; the deploy step's probes get canned answers).

The one invariant that matters (roborev 4747): once the live stack binds the
public hostname, an ordinary deploy that was not told about it must KEEP it —
never synthesize a distribution without the alias and detach production.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "infra" / "cdk" / "deploy.sh"

AWS_STUB = r'''#!/bin/sh
# Records every call; answers the few queries deploy.sh makes.
echo "aws $*" >>"$CALLS"
case "$*" in
  *"configure export-credentials"*) echo 'export AWS_ACCESS_KEY_ID=x; export AWS_SECRET_ACCESS_KEY=y; export AWS_SESSION_TOKEN=z' ;;
  *"sts get-caller-identity"*) echo "arn:aws:sts::012146975534:assumed-role/lou-deploy/test" ;;
  *"describe-stacks"*)
    [ -n "${WARN_STDERR:-}" ] && echo "urllib3 NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+" >&2
    case "${DESCRIBE_MODE:-ok}" in
      throttle) echo "An error occurred (Throttling) when calling the DescribeStacks operation: Rate exceeded" >&2; exit 254 ;;
      missing)  echo "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id LouStack does not exist" >&2; exit 254 ;;
    esac
    case "$*" in
      *"OutputKey=='PublicDomain'"*) printf '%s' "${LIVE_DOMAIN:-}" ;;
      *"OutputKey=='CertificateArn'"*) printf '%s' "${LIVE_CERT:-}" ;;
    esac ;;
  *) echo "" ;;
esac
'''
NPX_STUB = r'''#!/bin/sh
echo "npx $*" >>"$CALLS"
# deploy.sh runs `... deploy --require-approval never --outputs-file <OUT>` (no stack name).
case "$*" in *" deploy --require-approval"*) printf '%s' '{"LouStack":{"CloudFrontUrl":"https://d.test/","CloudFrontDomain":"d.test","FunctionName":"lou-bot"}}' >"$LOU_OUTPUTS_FILE" ;; esac
exit 0
'''
# curl/dig never leave the machine: the deploy step's post-deploy probes get
# canned answers so the unit suite cannot hit production or hang on timeouts.
CURL_STUB = r'''#!/bin/sh
echo "curl $*" >>"$CALLS"
case "$*" in
  *"%{http_code} %{content_type}"*) printf '200 text/event-stream; charset=utf-8' ;;
  *"%{http_code}"*) printf '200' ;;
  *) printf '{"status":"ok","tables":{"expenditures":1}}' ;;
esac
'''
DIG_STUB = r'''#!/bin/sh
echo "dig $*" >>"$CALLS"; printf '203.0.113.5\n'
'''


@pytest.fixture
def harness(tmp_path):
    bin_ = tmp_path / "bin"; bin_.mkdir()
    for name, body in (("aws", AWS_STUB), ("npx", NPX_STUB), ("curl", CURL_STUB), ("dig", DIG_STUB)):
        p = bin_ / name; p.write_text(body); p.chmod(0o755)
    data = tmp_path / "data"; data.mkdir()
    (data / "lou.duckdb").write_bytes(b"x"); (data / "rag_documents.duckdb").write_bytes(b"x")
    calls = tmp_path / "calls"

    def run(step="synth", env=None):
        calls.write_text("")
        e = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", CALLS=str(calls),
                 LOU_DATA_DIR=str(data), LOU_OUTPUTS_FILE=str(tmp_path / "outputs.json"), **(env or {}))
        e.pop("LOU_SKIP_PREVIEW", None) if not (env and "LOU_SKIP_PREVIEW" in env) else None
        e.pop("LOU_DOMAIN", None) if not (env and "LOU_DOMAIN" in env) else None
        proc = subprocess.run(["/bin/bash", str(SCRIPT), step], env=e, capture_output=True, text=True, timeout=60)
        return proc, calls.read_text()
    return run


def _cdk_args(calls):
    return [l for l in calls.splitlines() if l.startswith("npx ")]


def test_plain_deploy_keeps_the_live_hostname_binding(harness):
    proc, calls = harness("synth", env={"LIVE_DOMAIN": "louisville.raylytics.io",
                                        "LIVE_CERT": "arn:aws:acm:us-east-1:012146975534:certificate/abc"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "keeping the live public hostname louisville.raylytics.io" in proc.stdout
    npx = _cdk_args(calls)[0]
    assert "lou:domain=louisville.raylytics.io" in npx
    assert "lou:certificateArn=arn:aws:acm:us-east-1:012146975534:certificate/abc" in npx


def test_no_live_hostname_means_no_domain_context(harness):
    proc, calls = harness("synth")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "lou:domain" not in _cdk_args(calls)[0]


def test_explicit_env_overrides_and_drop_flag_detaches_deliberately(harness):
    live = {"LIVE_DOMAIN": "louisville.raylytics.io", "LIVE_CERT": "arn:aws:acm:us-east-1:012146975534:certificate/abc"}
    proc, calls = harness("synth", env={**live, "LOU_DOMAIN": "other.example", "LOU_CERT_ARN": "arn:x"})
    assert "lou:domain=other.example" in _cdk_args(calls)[0]
    proc, calls = harness("synth", env={**live, "LOU_DROP_DOMAIN": "1"})
    assert proc.returncode == 0
    assert "lou:domain" not in _cdk_args(calls)[0]


def test_live_domain_without_certificate_output_refuses(harness):
    proc, calls = harness("synth", env={"LIVE_DOMAIN": "louisville.raylytics.io", "LIVE_CERT": ""})
    assert proc.returncode == 1
    assert "refusing" in proc.stdout
    assert not _cdk_args(calls), "must not reach the CDK CLI"


def test_refuses_to_run_as_anything_but_the_deploy_role(harness, tmp_path):
    bad = tmp_path / "bin" / "aws"
    bad.write_text(AWS_STUB.replace("assumed-role/lou-deploy/test", "user/airflow-user"))
    proc, calls = harness("synth")
    assert proc.returncode == 1 and "refusing to run CDK as" in proc.stdout
    assert not _cdk_args(calls)


def test_output_lookup_failure_fails_closed(harness):
    """A throttle / expired session / permission error reading the stack's
    outputs must stop the run — treating it as "no hostname" would deploy a
    distribution without the alias (the silent detach, roborev 4749)."""
    proc, calls = harness("synth", env={"DESCRIBE_MODE": "throttle"})
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "cannot read LouStack outputs" in proc.stderr
    assert not _cdk_args(calls)


def test_missing_stack_is_the_only_no_domain_error(harness):
    """First deploy: the stack does not exist yet, which legitimately means
    no hostname is bound. The run proceeds without domain context."""
    proc, calls = harness("synth", env={"DESCRIBE_MODE": "missing"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "lou:domain" not in _cdk_args(calls)[0]


def test_cli_warnings_on_stderr_do_not_pollute_the_hostname(harness):
    """The AWS CLI warns on stderr while exiting 0 (LibreSSL, deprecations);
    the adopted value must be the bare hostname (roborev 4751)."""
    proc, calls = harness("synth", env={"WARN_STDERR": "1", "LIVE_DOMAIN": "louisville.raylytics.io",
                                        "LIVE_CERT": "arn:aws:acm:us-east-1:012146975534:certificate/abc"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    npx = _cdk_args(calls)[0]
    assert "-c lou:domain=louisville.raylytics.io -c" in npx, npx
    assert "NotOpenSSLWarning" not in npx


# ── the publish process: no production deploy without a local preview ──────

REPO = SCRIPT.parent.parent.parent


def _head():
    return subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()


@pytest.fixture
def marker():
    path = REPO / ".preview-ok"
    before = path.read_text() if path.exists() else None
    yield path
    if before is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(before)


def test_deploy_refuses_without_a_preview_of_this_commit(harness, marker):
    marker.unlink(missing_ok=True)
    proc, calls = harness("deploy")
    assert proc.returncode == 1
    assert "has not been previewed" in proc.stdout and "infra/preview.sh" in proc.stdout
    assert not _cdk_args(calls), "must not reach the CDK CLI"


def test_deploy_refuses_a_marker_for_another_commit(harness, marker):
    marker.write_text("0000000000000000000000000000000000000000\n")
    proc, calls = harness("deploy")
    assert proc.returncode == 1 and "has not been previewed" in proc.stdout
    assert not _cdk_args(calls)


def test_deploy_proceeds_with_a_marker_for_head(harness, marker):
    """With the marker matching HEAD (and a clean tree) the gate opens; the
    stubbed CDK CLI is reached. The later steps need real outputs and are
    not this test's concern."""
    if subprocess.run(["git", "-C", str(REPO), "diff", "--quiet"]).returncode != 0:
        pytest.skip("working tree is dirty; the gate would (correctly) refuse")
    marker.write_text(_head() + "\n")
    proc, calls = harness("deploy")
    assert "approved locally" in proc.stdout
    assert any("aws-cdk@2 deploy --require-approval never --outputs-file" in c for c in _cdk_args(calls)), calls
    # The whole post-deploy verification ran against the stubs, not the network.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DEPLOY VERIFIED: https://d.test/" in proc.stdout
    assert all(("d.test" in c or "127.0.0.1" in c) for c in calls.splitlines() if c.startswith("curl ")), calls


def test_emergency_bypass_is_loud(harness, marker):
    marker.unlink(missing_ok=True)
    proc, calls = harness("deploy", env={"LOU_SKIP_PREVIEW": "1"})
    assert "deploying WITHOUT a local preview" in proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DEPLOY VERIFIED" in proc.stdout
