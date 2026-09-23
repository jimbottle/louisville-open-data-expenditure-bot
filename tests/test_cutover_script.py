"""infra/cdk/cutover.sh against stubbed dig/aws binaries (hermetic).

Pins the two things the first real run got wrong (roborev 4757): CAA is
evaluated the way ACM evaluates it — the first non-empty CAA set walking up
from the name is authoritative, CNAME rows are not CAA rows — and a stored
certificate in FAILED status is discarded and re-requested instead of having
its dead validation record printed.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "infra" / "cdk" / "cutover.sh"

# `dig +noall +answer -t CAA <name>` output is driven by env: CAA_<label> holds
# the answer lines for that exact name (empty = NXDOMAIN/no answer).
DIG_STUB = r'''#!/bin/sh
echo "dig $*" >>"$CALLS"
name=""; for a in "$@"; do name=$a; done
key=$(printf '%s' "$name" | tr '.-' '__')
eval "printf '%s\n' \"\${CAA_$key:-}\""
'''
AWS_STUB = r'''#!/bin/sh
echo "aws $*" >>"$CALLS"
case "$*" in
  *"describe-certificate"*"Certificate.[Status,FailureReason]"*)
    case "${DESCRIBE_MODE:-ok}" in
      notfound) echo "An error occurred (ResourceNotFoundException) when calling the DescribeCertificate operation: Could not find certificate" >&2; exit 254 ;;
      expired)  echo "An error occurred (ExpiredToken) when calling the DescribeCertificate operation: The security token included in the request is expired" >&2; exit 254 ;;
    esac
    printf '%s\t%s' "${CERT_STATUS:-ISSUED}" "${CERT_REASON:-None}" ;;
  *"request-certificate"*) printf 'arn:aws:acm:us-east-1:012146975534:certificate/new-one' ;;
  *"ResourceRecord.[Name,Value]"*) printf '_abc.louisville.raylytics.io.\t_xyz.acm-validations.aws.' ;;
  *) echo "" ;;
esac
'''


@pytest.fixture
def harness(tmp_path):
    bin_ = tmp_path / "bin"; bin_.mkdir()
    for name, body in (("dig", DIG_STUB), ("aws", AWS_STUB)):
        p = bin_ / name; p.write_text(body); p.chmod(0o755)
    calls = tmp_path / "calls"
    state = tmp_path / "state.json"

    def run(step, env=None):
        calls.write_text("")
        e = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", CALLS=str(calls),
                 LOU_CUTOVER_STATE=str(state), **(env or {}))
        proc = subprocess.run(["/bin/bash", str(SCRIPT), step], env=e, capture_output=True, text=True, timeout=60)
        return proc, calls.read_text()
    run.state = state
    return run


APEX_ALLOWS = 'raylytics.io.\t300\tIN\tCAA\t0 issue "amazon.com"\nraylytics.io.\t300\tIN\tCAA\t0 issue "letsencrypt.org"'
APEX_DENIES = 'raylytics.io.\t300\tIN\tCAA\t0 issue "letsencrypt.org"\nraylytics.io.\t300\tIN\tCAA\t0 issue "digicert.com"'


def test_caa_passes_when_the_apex_allows_amazon(harness):
    proc, _ = harness("caa-check", env={"CAA_raylytics_io": APEX_ALLOWS})
    assert proc.returncode == 0, proc.stdout
    assert 'issue "amazon.com"' in proc.stdout


def test_caa_refuses_when_no_set_names_amazon(harness):
    proc, _ = harness("caa-check", env={"CAA_raylytics_io": APEX_DENIES})
    assert proc.returncode == 1
    assert "Amazon is not an allowed issuer" in proc.stdout


def test_caa_uses_the_first_set_walking_up_not_a_union(harness):
    """A CAA set on the hostname itself is authoritative; an apex that allows
    Amazon does not rescue it (RFC 8659), and vice versa."""
    sub_denies = 'louisville.raylytics.io.\t300\tIN\tCAA\t0 issue "letsencrypt.org"'
    proc, _ = harness("caa-check", env={"CAA_louisville_raylytics_io": sub_denies, "CAA_raylytics_io": APEX_ALLOWS})
    assert proc.returncode == 1, proc.stdout
    assert "authoritative set at louisville.raylytics.io" in proc.stdout
    sub_allows = 'louisville.raylytics.io.\t300\tIN\tCAA\t0 issue "amazontrust.com"'
    proc, _ = harness("caa-check", env={"CAA_louisville_raylytics_io": sub_allows, "CAA_raylytics_io": APEX_DENIES})
    assert proc.returncode == 0, proc.stdout


def test_caa_ignores_cname_rows_and_passes_with_no_caa_anywhere(harness):
    """After cutover the hostname is a DNS-only CNAME to CloudFront; dig's
    answer section then carries a CNAME row, which is not a CAA record."""
    cname = 'louisville.raylytics.io.\t300\tIN\tCNAME\td123.cloudfront.net.'
    proc, _ = harness("caa-check", env={"CAA_louisville_raylytics_io": cname})
    assert proc.returncode == 0, proc.stdout
    assert "no records" in proc.stdout and "cloudfront" not in proc.stdout.split("no records")[0]


def test_caa_issue_semicolon_denies_everyone(harness):
    proc, _ = harness("caa-check", env={"CAA_raylytics_io": 'raylytics.io.\t300\tIN\tCAA\t0 issue ";"'})
    assert proc.returncode == 1


def test_cert_discards_a_failed_stored_certificate_and_requests_anew(harness):
    harness.state.write_text('{"certificate_arn": "arn:aws:acm:us-east-1:012146975534:certificate/dead"}')
    proc, calls = harness("cert", env={"CERT_STATUS": "FAILED", "CERT_REASON": "CAA_ERROR",
                                       "CAA_raylytics_io": APEX_ALLOWS})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "is FAILED (CAA_ERROR); requesting anew" in proc.stdout
    assert "request-certificate" in calls
    assert "certificate/new-one" in harness.state.read_text()
    assert "_abc.louisville.raylytics.io" in proc.stdout


def test_cert_keeps_a_pending_stored_certificate(harness):
    harness.state.write_text('{"certificate_arn": "arn:aws:acm:us-east-1:012146975534:certificate/pending"}')
    proc, calls = harness("cert", env={"CERT_STATUS": "PENDING_VALIDATION", "CAA_raylytics_io": APEX_ALLOWS})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "request-certificate" not in calls
    assert "certificate/pending" in harness.state.read_text()


def test_cert_refuses_to_request_against_a_denying_caa(harness):
    proc, calls = harness("cert", env={"CAA_raylytics_io": APEX_DENIES})
    assert proc.returncode == 1
    assert "request-certificate" not in calls


def test_cert_lookup_error_keeps_the_stored_certificate(harness):
    """An expired session / throttle must NOT discard a good certificate: the
    run stops, the state file is untouched, nothing is requested."""
    harness.state.write_text('{"certificate_arn": "arn:aws:acm:us-east-1:012146975534:certificate/good"}')
    proc, calls = harness("cert", env={"DESCRIBE_MODE": "expired", "CAA_raylytics_io": APEX_ALLOWS})
    assert proc.returncode == 1
    assert "cannot read certificate" in proc.stderr
    assert "request-certificate" not in calls
    assert "certificate/good" in harness.state.read_text()


def test_cert_not_found_is_treated_as_gone(harness):
    harness.state.write_text('{"certificate_arn": "arn:aws:acm:us-east-1:012146975534:certificate/deleted"}')
    proc, calls = harness("cert", env={"DESCRIBE_MODE": "notfound", "CAA_raylytics_io": APEX_ALLOWS})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "is MISSING (not found); requesting anew" in proc.stdout
    assert "request-certificate" in calls and "certificate/new-one" in harness.state.read_text()
