# Least-privilege IAM setup for the Lou migration

Creates one **user** (`lou-dev`), one **role** (`lou-deploy`), and three managed
policies, scoped to only what the migration in `LOU_MIGRATION_COMPAT.md`
actually needs. Tracked as `louisville-open-data-n0b`.

> **Why three policies and not two.** IAM caps a managed policy at **6,144
> characters** and does *not* count whitespace, so reformatting cannot rescue an
> oversize policy. The deploy ceiling came to 6,680, so it is split by concern:
> `LouDeployServices` (what may be built — the half that grows as deploys
> surface missing actions) and `LouDeployGuardrails` (IAM/STS grants and every
> Deny — deliberately small, changes rarely). Both are attached together and
> IAM evaluates them as one identity policy, and an explicit Deny in either
> overrides an Allow in the other, so the split is safe.

**Why this is tighter than usual:** the target account is *shared* with an
unrelated Airflow workload. Isolation is by convention (`lou-*` names,
`/lou/` IAM path, `Project=lou` tags), so it has to be enforced in IAM rather
than assumed.

## The permission chain

```
you (admin, one time)      creates the policies, user, role; runs cdk bootstrap
        │
   lou-dev  (IAM user)     can do NOTHING but assume lou-deploy, MFA required
        │  sts:AssumeRole
   lou-deploy  (IAM role)  LouDeployServices + LouDeployGuardrails
        │  sts:AssumeRole
   cdk-lou0-* roles        created by bootstrap, Lou's own qualifier
        │
   CloudFormation          executes as cfn-exec-role, capped by the SAME two policies
        │  creates, with LouPermissionsBoundary forced on
   lou-lambda-exec         the runtime ceiling — logs, its own tables, its own secrets
```

Two ceilings, deliberately different sizes: **LouDeployServices +
LouDeployGuardrails** is what may be *built*; **LouPermissionsBoundary** is what
the running function may *do*. The
boundary is what makes granting `iam:CreateRole` safe — without it, a principal
that can create roles can create an admin role and assume it.

## Naming rules the policies depend on

Isolation is by name, so these are not cosmetic — a resource named outside the
pattern is simply denied (fail closed).

| Resource | Pattern | Example |
|---|---|---|
| Lambda, DynamoDB, ECR, alarms, schedules | `lou-*` | `lou-ask`, `lou-response-cache` |
| CloudFormation stacks | `Lou*` | `LouStack` |
| IAM roles | path `/lou/` | `/lou/lou-lambda-exec` |
| SSM parameters | `/lou/*` | `/lou/prod/OPENROUTER_API_KEY` |
| Log groups | `/aws/lambda/lou-*` or `/lou/*` | `/aws/lambda/lou-ask` |
| **S3 buckets** | **`lou-<purpose>-<ACCOUNT_ID>-us-east-1`** | `lou-data-012345678901-us-east-1` |

**S3 is the special case.** Bucket ARNs (`arn:aws:s3:::name`) contain no account
ID, and the bucket namespace is global across *all* AWS accounts — so
`arn:aws:s3:::lou-*` would also match a `lou-`-prefixed bucket owned by someone
else entirely. Two defenses:

1. Bucket names carry the account ID and region, the same convention CDK uses
   for its own assets bucket.
2. An `S3ResourceAccountPin` **Deny** on `aws:ResourceAccount != <account>`,
   present in `LouDeployGuardrails` **and** independently in
   `LouPermissionsBoundary`. This is the control that actually holds — it
   survives a misnamed bucket, and closes the "someone registers `lou-evil` and
   your role has `s3:*` against it" hole. `StringNotEqualsIfExists` so that
   account-level calls which do not populate the key (`ListAllMyBuckets`) are
   not caught by it.

   It appears in both because they are **separate evaluation contexts**: the
   two deploy policies are attached together and evaluated as one identity
   policy, but a permissions boundary is intersected with the identity policy
   rather than merged into it — so a pin on the deploy side does nothing for
   the Lambda execution role. `tests/test_iam_policies.py` asserts this per
   context, not per file.

In CDK, set the bucket name explicitly rather than letting it auto-generate:

```python
bucket_name=f"lou-data-{Stack.of(self).account}-{Stack.of(self).region}"
```

---

## Step 0 — Prerequisites

Run steps 1–6 as an **account administrator**, not as `lou-dev`. `lou-deploy`
deliberately cannot create IAM users, and cannot bootstrap CDK (bootstrap makes
roles outside the `/lou/` path). This is a one-time human action.

```bash
aws sts get-caller-identity          # confirm you are the admin, not airflow-user
export AWS_REGION=us-east-1
```

## Step 1 — Render the policies with your account ID

The templates are committed with `${AWS_ACCOUNT_ID}` because **this repository
is public**. An account ID is not a credential, but it is a confirmed target
for cross-account trust probing, so it stays out of git.

```bash
./infra/iam/render.sh                # writes infra/iam/rendered/ (gitignored)
```

### Optional: lint the policies before creating them

IAM Access Analyzer catches malformed conditions and unused/invalid actions.
Worth one pass, since a wrong condition key fails *open* (the condition is
ignored) rather than erroring:

```bash
for f in infra/iam/rendered/lou-deploy-services.json \
         infra/iam/rendered/lou-deploy-guardrails.json \
         infra/iam/rendered/lou-permissions-boundary.json; do
  aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY \
    --policy-document "file://$f" --query 'findings[].{Type:findingType,Detail:findingDetails}'
done
```

> Left for you to run rather than done automatically: `accessanalyzer` is not on
> the agent read-only allowlist, and while `ValidatePolicy` itself is not a
> billed check, other Access Analyzer APIs are — so it falls under "ask first"
> per the AWS interaction policy in `CLAUDE.md`.

## Step 2 — Create the three managed policies

Boundary first: `LouDeployGuardrails` references it by ARN in a Deny.

```bash
aws iam create-policy \
  --policy-name LouPermissionsBoundary \
  --policy-document file://infra/iam/rendered/lou-permissions-boundary.json \
  --description "Runtime ceiling for roles created by the Lou stack"

aws iam create-policy \
  --policy-name LouDeployServices \
  --policy-document file://infra/iam/rendered/lou-deploy-services.json \
  --description "Deploy-time ceiling, part 1 of 2: services Lou is built from"

aws iam create-policy \
  --policy-name LouDeployGuardrails \
  --policy-document file://infra/iam/rendered/lou-deploy-guardrails.json \
  --description "Deploy-time ceiling, part 2 of 2: IAM grants and every Deny"
```

> If you already created `LouServicePolicy` from an earlier revision, delete it
> first — it is superseded by these two:
> `aws iam delete-policy --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/LouServicePolicy`

## Step 3 — Create the role `lou-deploy`

```bash
aws iam create-role \
  --role-name lou-deploy \
  --assume-role-policy-document file://infra/iam/rendered/lou-deploy-trust.json \
  --max-session-duration 3600 \
  --description "Deploy principal for the Lou serverless migration" \
  --tags Key=Project,Value=lou

aws iam attach-role-policy \
  --role-name lou-deploy \
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/LouDeployServices

aws iam attach-role-policy \
  --role-name lou-deploy \
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/LouDeployGuardrails
```

Both are required. `LouDeployServices` alone grants no IAM and carries none of
the Deny guardrails; `LouDeployGuardrails` alone can build nothing.

## Step 4 — Create the user `lou-dev`

```bash
aws iam create-user --user-name lou-dev --tags Key=Project,Value=lou

aws iam put-user-policy \
  --user-name lou-dev \
  --policy-name lou-dev-assume-only \
  --policy-document file://infra/iam/rendered/lou-dev-user-policy.json
```

**Enable MFA before creating an access key.** The trust policy requires
`aws:MultiFactorAuthPresent`, so a key without MFA can assume nothing — that is
the point, and it means a leaked key is close to worthless.

```bash
# Console: IAM > Users > lou-dev > Security credentials > Assign MFA device
aws iam create-access-key --user-name lou-dev      # capture the secret ONCE
```

> Store the secret in your password manager. Do not paste it into this repo,
> into a bd issue, or into a chat session.

## Step 5 — Configure the local profile

Append to `~/.aws/config`:

```ini
[profile lou-dev]
region = us-east-1

[profile lou]
region         = us-east-1
role_arn       = arn:aws:iam::<ACCOUNT_ID>:role/lou-deploy
source_profile = lou-dev
mfa_serial     = arn:aws:iam::<ACCOUNT_ID>:mfa/lou-dev
duration_seconds = 3600
```

Put the `lou-dev` key in `~/.aws/credentials` under `[lou-dev]`. Then verify —
this should print the **assumed-role** ARN, not the user:

```bash
aws sts get-caller-identity --profile lou
# expect: arn:aws:sts::<ACCOUNT_ID>:assumed-role/lou-deploy/botocore-session-...
```

## Step 6 — Bootstrap CDK, scoped (admin runs this)

The default `cdk bootstrap` grants its execution role **AdministratorAccess**,
which would silently undo everything above. Override it, and use a dedicated
qualifier so Lou gets its own bootstrap roles rather than sharing a set with
anything else in this account.

```bash
npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1 \
  --qualifier lou0 \
  --toolkit-stack-name CDKToolkit-Lou \
  --cloudformation-execution-policies arn:aws:iam::<ACCOUNT_ID>:policy/LouDeployServices,arn:aws:iam::<ACCOUNT_ID>:policy/LouDeployGuardrails \
  --custom-permissions-boundary LouPermissionsBoundary \
  --trust <ACCOUNT_ID>
```

The CDK app must then use the same qualifier, or it will look for
default-named bootstrap resources and fail:

```python
# infra/app.py
DefaultStackSynthesizer(qualifier="lou0")
```

> **Cost note (Tier 2):** bootstrap creates an S3 bucket and an ECR repository.
> Empty they are effectively free; the ECR repo becomes the ~$0.06/month line
> once Lou's ~600 MB image lands, after the 12-month 500 MB free tier.

## Step 7 — Verify the boundaries actually hold

Negative tests. Each should be **denied** — if any succeeds, the scoping is
wrong and worth fixing before you build on it.

```bash
# Outside the lou-* namespace
aws dynamodb create-table --profile lou --table-name airflow-should-fail \
  --attribute-definitions AttributeName=k,AttributeType=S \
  --key-schema AttributeName=k,KeyType=HASH --billing-mode PAY_PER_REQUEST

# Privilege escalation via a role outside the /lou/ path
aws iam create-role --profile lou --role-name escalate-should-fail \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

# The architecture forbids these outright
aws ec2 create-vpc --profile lou --cidr-block 10.0.0.0/16

# A bucket without the account suffix — this is the invariant that regressed
# twice (see the Naming rules section). It must be denied, NOT created.
aws s3api create-bucket --profile lou --bucket lou-badname-should-fail
```

And one that should **succeed**:

```bash
aws dynamodb create-table --profile lou --table-name lou-smoke-test \
  --attribute-definitions AttributeName=k,AttributeType=S \
  --key-schema AttributeName=k,KeyType=HASH --billing-mode PAY_PER_REQUEST
aws dynamodb delete-table --profile lou --table-name lou-smoke-test
```

---

## Where `Resource: "*"` was unavoidable

Least privilege is a target, not an absolute — some AWS actions have no
resource-level form. Each of these is a deliberate, bounded exception:

| Statement | Why `*` | Compensating control |
|---|---|---|
| `CloudFrontHasNoResourceLevelPermissions…` | CloudFront is a global service; `CreateDistribution` / `CreateOriginAccessControl` accept no resource ARN | Verb list is explicit — no `cloudfront:*`. Distributions are tagged `Project=lou` |
| `WafV2ForCloudFront…` | WAF for CloudFront lives in the global scope; create/list take no ARN | Explicit verb list, no `wafv2:*` |
| `EcrAuthTokenHasNoResourceScope` | `ecr:GetAuthorizationToken` is account-level by definition | Every other ECR verb is repo-scoped |
| `S3ListAllMyBuckets`, `DynamoDBListTables`, `LogsDescribe*`, `CloudWatch` reads | List/describe operations are account-level | Read-only. Reveals Airflow resource *names*, not contents |
| `KmsDecrypt…` | The SSM-managed key ARN is not known ahead of time | `kms:ViaService` condition — usable **only** through SSM, not directly |

The remaining risk is disclosure of resource names in the shared account, not
modification. If that matters, a separate account removes it — the tradeoff you
weighed in `louisville-open-data-n0b`.

## Expect to iterate once

A hand-written least-privilege policy essentially never survives first contact.
The first `cdk deploy` will likely surface one to three missing actions. That is
normal; do **not** respond by widening to `Action: "*"`.

```bash
npx cdk deploy --profile lou 2>&1 | grep -i "not authorized\|AccessDenied"
```

Read the action out of the error, add exactly that action to the matching
statement in `lou-deploy-services.json` (missing actions almost always belong
there rather than in the guardrails), re-render, and update the policy:

```bash
./infra/iam/render.sh
aws iam create-policy-version \
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/LouDeployServices \
  --policy-document file://infra/iam/rendered/lou-deploy-services.json \
  --set-as-default
```

Commit the template change so the policy stays reviewable in git.

## Teardown

```bash
npx cdk destroy --profile lou                 # Lou's own stacks
aws cloudformation delete-stack --stack-name CDKToolkit-Lou
aws iam delete-user --user-name lou-dev       # delete access keys first
aws iam delete-role --role-name lou-deploy    # detach both policies first
```

Because everything is `lou-*` / `Lou*` / `/lou/`, teardown cannot reach the
Airflow workload — which is the property the naming convention exists to buy.
