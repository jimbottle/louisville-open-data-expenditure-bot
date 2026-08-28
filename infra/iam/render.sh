#!/usr/bin/env bash
# Render the IAM policy templates with a real account ID.
#
# The templates carry ${AWS_ACCOUNT_ID} rather than a literal because this
# repository is PUBLIC (see louisville-open-data-15m). An account ID is not a
# credential, but publishing one hands an attacker a confirmed target for
# cross-account trust probing and role-name enumeration — so it stays out of git.
#
# Usage:  ./infra/iam/render.sh            # uses the CURRENT caller's account
#         AWS_ACCOUNT_ID=123456789012 ./infra/iam/render.sh
#
# Output goes to infra/iam/rendered/, which is gitignored.
set -euo pipefail

cd "$(dirname "$0")"
OUT=rendered
mkdir -p "$OUT"

if [ -z "${AWS_ACCOUNT_ID:-}" ]; then
  AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  echo "Using account from current credentials: $AWS_ACCOUNT_ID"
fi

if ! printf '%s' "$AWS_ACCOUNT_ID" | grep -Eq '^[0-9]{12}$'; then
  echo "AWS_ACCOUNT_ID must be 12 digits, got: $AWS_ACCOUNT_ID" >&2
  exit 1
fi

for f in lou-dev-user-policy lou-deploy-trust lou-service-policy lou-permissions-boundary; do
  # Drop the "Comment" keys: they document intent here but IAM rejects them.
  python3 - "$f.json" "$OUT/$f.json" "$AWS_ACCOUNT_ID" <<'PY'
import json, sys
src, dst, acct = sys.argv[1], sys.argv[2], sys.argv[3]
raw = open(src).read().replace("${AWS_ACCOUNT_ID}", acct)
doc = json.loads(raw)
doc.pop("Comment", None)
for stmt in doc.get("Statement", []):
    stmt.pop("Comment", None)
json.dump(doc, open(dst, "w"), indent=2)
PY
  echo "  rendered $OUT/$f.json"
done

echo
echo "Done. Next: follow infra/iam/README.md from step 2."
echo "These rendered files contain your account ID — they are gitignored, keep them local."
