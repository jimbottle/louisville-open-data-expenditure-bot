"""IAM policy invariants for the AWS migration (infra/iam/).

These assert SECURITY PROPERTIES, not the literal contents of the JSON — a
statement can be renamed or reworded freely, but it cannot stop being
account-scoped without failing here.

Why this file exists: the same bug shipped twice. `arn:aws:s3:::lou-*` was
committed unscoped in 4daa353, fixed in 8facd8a, and both times it was caught
only by a human reading the diff. S3 ARNs carry no account field and the bucket
namespace is global across ALL AWS accounts, so a bare `lou-*` also matches a
bucket owned by someone else entirely. Nothing mechanically stopped a third
occurrence, so: this.

Pure JSON parsing — no AWS credentials, no network, no data files.
"""
import glob
import json
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IAM_DIR = os.path.join(REPO, "infra", "iam")

PLACEHOLDER = "${AWS_ACCOUNT_ID}"

# The two policies that grant permissions. The trust policy and the user policy
# are checked separately: they have different shapes.
GRANTING_POLICIES = ["lou-service-policy.json", "lou-permissions-boundary.json"]
ALL_POLICIES = GRANTING_POLICIES + ["lou-dev-user-policy.json", "lou-deploy-trust.json"]


def _load(name: str) -> dict:
    with open(os.path.join(IAM_DIR, name)) as f:
        return json.load(f)


def _statements(doc: dict) -> list:
    return doc.get("Statement", [])


def _resources(stmt: dict) -> list:
    r = stmt.get("Resource", [])
    return [r] if isinstance(r, str) else list(r)


def _actions(stmt: dict) -> list:
    a = stmt.get("Action", [])
    return [a] if isinstance(a, str) else list(a)


def _condition_text(stmt: dict) -> str:
    return json.dumps(stmt.get("Condition", {}))


# ── Structural sanity ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ALL_POLICIES)
def test_policy_is_valid_json_with_statements(name):
    doc = _load(name)
    assert doc.get("Version") == "2012-10-17"
    assert _statements(doc), f"{name} has no statements"


@pytest.mark.parametrize("name", ALL_POLICIES)
def test_templates_never_hardcode_an_account_id(name):
    """This repository is PUBLIC (louisville-open-data-15m). An account ID is
    not a credential, but it is a confirmed target for cross-account trust
    probing and role-name enumeration, so the templates carry a placeholder and
    render.sh substitutes the real value into a gitignored directory."""
    raw = open(os.path.join(IAM_DIR, name)).read()
    leaked = re.findall(r"(?<![\d-])\d{12}(?![\d-])", raw)
    assert not leaked, f"{name} contains a literal 12-digit account ID: {leaked}"


# ── The invariant that broke twice ───────────────────────────────────────────

@pytest.mark.parametrize("name", GRANTING_POLICIES)
def test_every_s3_arn_is_account_scoped_by_name(name):
    """S3 ARNs have no account field, so the account must appear in the BUCKET
    NAME. A bare arn:aws:s3:::lou-* matches another customer's bucket."""
    offenders = []
    for stmt in _statements(_load(name)):
        if stmt.get("Effect") != "Allow":
            continue  # a Deny that is over-broad is fail-safe, not fail-open
        for r in _resources(stmt):
            if r.startswith("arn:aws:s3:::") and PLACEHOLDER not in r:
                offenders.append((stmt.get("Sid"), r))
    assert not offenders, (
        f"{name}: S3 ARNs not pinned to the account (bucket names are globally "
        f"unique, so these match buckets in ANY account): {offenders}"
    )


@pytest.mark.parametrize("name", GRANTING_POLICIES)
def test_s3_access_is_denied_outside_this_account(name):
    """Defence in depth for the above: the name convention still fails OPEN if
    a bucket is ever misnamed. A Deny on aws:ResourceAccount does not."""
    pins = [
        s for s in _statements(_load(name))
        if s.get("Effect") == "Deny"
        and any(a == "s3:*" or a.startswith("s3:") for a in _actions(s))
        and "aws:ResourceAccount" in _condition_text(s)
    ]
    assert pins, (
        f"{name}: no Deny pinning s3 actions to aws:ResourceAccount. Without it, "
        "a misnamed bucket silently regains cross-account reach."
    )
    for p in pins:
        assert "StringNotEqualsIfExists" in _condition_text(p), (
            f"{name} [{p.get('Sid')}]: use StringNotEqualsIfExists, not "
            "StringNotEquals — a plain StringNotEquals also fires when the key "
            "is absent, which breaks account-level calls like ListAllMyBuckets."
        )
        assert PLACEHOLDER in _condition_text(p)


@pytest.mark.parametrize("name", GRANTING_POLICIES)
def test_non_s3_arns_carry_an_account_field(name):
    """Catches a NEW service added without scoping — the general form of the
    bug. Resource "*" is exempt: several AWS actions (CloudFront, WAF,
    ecr:GetAuthorizationToken, list/describe) have no resource-level form, and
    each such case is documented in infra/iam/README.md."""
    offenders = []
    for stmt in _statements(_load(name)):
        for r in _resources(stmt):
            if r == "*" or r.startswith("arn:aws:s3:::"):
                continue
            parts = r.split(":")
            account_field = parts[4] if len(parts) > 4 else ""
            if PLACEHOLDER not in account_field:
                offenders.append((stmt.get("Sid"), r))
    assert not offenders, f"{name}: ARNs with no account in the account field: {offenders}"


# ── Privilege-escalation controls ────────────────────────────────────────────

def test_created_roles_must_carry_the_permissions_boundary():
    """The deploy role can create IAM roles, which is normally an escalation
    path straight to admin. What closes it is the Deny requiring the boundary
    on every role it creates. Remove that and iam:CreateRole becomes iam:*."""
    denies = [
        s for s in _statements(_load("lou-service-policy.json"))
        if s.get("Effect") == "Deny"
        and "iam:PermissionsBoundary" in _condition_text(s)
        and any(a == "iam:CreateRole" for a in _actions(s))
    ]
    assert denies, (
        "lou-service-policy.json: nothing forces LouPermissionsBoundary onto "
        "roles created by the deploy principal — iam:CreateRole is then an "
        "unbounded privilege-escalation path."
    )
    assert any("StringNotEquals" in _condition_text(d) for d in denies)


def test_the_boundary_itself_cannot_be_edited_or_detached():
    """A boundary the holder can rewrite is not a boundary."""
    txt = json.dumps(_load("lou-service-policy.json"))
    protecting = [
        s for s in _statements(_load("lou-service-policy.json"))
        if s.get("Effect") == "Deny"
        and any("LouPermissionsBoundary" in r for r in _resources(s))
    ]
    assert protecting, "nothing prevents editing/deleting LouPermissionsBoundary"
    guarded = {a for s in protecting for a in _actions(s)}
    for action in ("iam:CreatePolicyVersion", "iam:DeleteRolePermissionsBoundary"):
        assert action in guarded, f"{action} not denied on the boundary policy"
    assert "LouPermissionsBoundary" in txt


def test_iam_writes_are_confined_to_the_lou_path():
    """Isolation in a shared account is by name/path, so IAM writes must not
    reach roles belonging to the co-tenant Airflow workload."""
    for stmt in _statements(_load("lou-service-policy.json")):
        if stmt.get("Effect") != "Allow":
            continue
        writes = [a for a in _actions(stmt)
                  if a.startswith("iam:") and a != "iam:PassRole"
                  and any(a.startswith(f"iam:{v}") for v in
                          ("Create", "Delete", "Update", "Put", "Attach", "Detach", "Tag", "Untag"))]
        if not writes:
            continue
        for r in _resources(stmt):
            assert ":role/lou/" in r, (
                f"IAM write {writes} granted on {r}, which is outside the /lou/ path"
            )


def test_passrole_is_restricted_to_lou_roles_and_named_services():
    """Unrestricted iam:PassRole lets a principal hand any role to a service it
    controls, which is escalation by another route."""
    passers = [s for s in _statements(_load("lou-service-policy.json"))
               if s.get("Effect") == "Allow" and "iam:PassRole" in _actions(s)]
    assert passers, "no iam:PassRole grant found (the stack needs one)"
    for s in passers:
        assert all(":role/lou/" in r for r in _resources(s)), \
            "iam:PassRole is not confined to /lou/ roles"
        assert "iam:PassedToService" in _condition_text(s), \
            "iam:PassRole has no PassedToService condition"


# ── Runtime ceiling ──────────────────────────────────────────────────────────

def test_runtime_boundary_forbids_escalation_and_lateral_movement():
    """The boundary is the ceiling for the Lambda execution role. It must not
    permit the function to mint permissions or assume its way sideways."""
    denied = set()
    for s in _statements(_load("lou-permissions-boundary.json")):
        if s.get("Effect") == "Deny":
            denied.update(_actions(s))
    for action in ("iam:*", "sts:AssumeRole"):
        assert action in denied, f"runtime boundary does not deny {action}"


def test_runtime_boundary_grants_no_wildcard_service_access():
    """Every Allow in the boundary should name actions explicitly. A service
    wildcard here would silently widen the runtime ceiling."""
    offenders = []
    for s in _statements(_load("lou-permissions-boundary.json")):
        if s.get("Effect") != "Allow":
            continue
        for a in _actions(s):
            if a == "*" or (a.endswith(":*")):
                offenders.append((s.get("Sid"), a))
    assert not offenders, f"wildcard Allow actions in the runtime boundary: {offenders}"


def test_architecture_forbids_vpc_and_nat():
    """The cost model depends on there being no NAT Gateway (~$32/mo against a
    ~$0.15 envelope). Denying it in IAM makes that a guardrail, not a habit."""
    denied = set()
    for s in _statements(_load("lou-service-policy.json")):
        if s.get("Effect") == "Deny":
            denied.update(_actions(s))
    assert "ec2:CreateNatGateway" in denied
    assert "ec2:CreateVpc" in denied


# ── Identity chain ───────────────────────────────────────────────────────────

def test_the_user_can_only_assume_the_role_and_needs_mfa():
    """lou-dev holds no standing permissions, so a leaked long-lived key is
    worth nothing without the MFA-gated assumption."""
    doc = _load("lou-dev-user-policy.json")
    assume = [s for s in _statements(doc) if "sts:AssumeRole" in _actions(s)]
    assert assume, "lou-dev cannot assume lou-deploy"
    for s in assume:
        assert "MultiFactorAuthPresent" in _condition_text(s)
        assert all("role/lou-deploy" in r for r in _resources(s))
    # No statement may grant anything beyond assumption and self-service MFA.
    for s in _statements(doc):
        for a in _actions(s):
            assert a == "sts:AssumeRole" or a.startswith("iam:"), \
                f"lou-dev granted unexpected action {a}"


def test_the_deploy_role_is_not_assumable_by_the_whole_account():
    """Trusting the account root would let the co-tenant airflow-user assume
    lou-deploy. The trust must name the user."""
    for s in _statements(_load("lou-deploy-trust.json")):
        principals = json.dumps(s.get("Principal", {}))
        assert ":root" not in principals, \
            "lou-deploy trusts the account root — any principal in the shared account could assume it"
        assert "user/lou-dev" in principals
        assert "MultiFactorAuthPresent" in _condition_text(s)
