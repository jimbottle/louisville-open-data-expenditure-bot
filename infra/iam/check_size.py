#!/usr/bin/env python3
"""Fail if a rendered IAM policy exceeds the managed-policy size limit.

IAM caps a managed policy at 6144 characters and does NOT count whitespace, so
reformatting or minifying cannot rescue an oversize policy — only removing
content or splitting the policy can. That is why LouDeployServices and
LouDeployGuardrails are two policies rather than one: combined they were 6680.

Run by render.sh so the failure surfaces before `aws iam create-policy` returns
`LimitExceeded` partway through the setup steps.

Usage: check_size.py <rendered-dir>
"""
import glob
import json
import os
import re
import sys

LIMIT = 6144
WARN_AT = 5500  # leave headroom: deploys routinely surface one or two missing actions


def policy_size(path: str) -> int:
    """Character count as IAM measures it: whitespace excluded."""
    return len(re.sub(r"\s", "", open(path).read()))


def main(argv: list) -> int:
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2

    files = sorted(glob.glob(os.path.join(argv[1], "*.json")))
    if not files:
        print(f"No rendered policies found in {argv[1]}", file=sys.stderr)
        return 2

    failed = False
    for f in files:
        n = policy_size(f)
        name = os.path.basename(f)
        # Trust policies are attached inline and are not managed policies, but
        # they are far smaller than the limit anyway, so one check covers all.
        if n > LIMIT:
            print(f"  ERROR {name}: {n} chars, over the IAM {LIMIT} limit by "
                  f"{n - LIMIT}. Whitespace is not counted, so reformatting will "
                  f"not help — move statements into another policy.", file=sys.stderr)
            failed = True
        elif n > WARN_AT:
            print(f"  WARN  {name}: {n} chars, approaching the IAM {LIMIT} limit "
                  f"({LIMIT - n} spare)", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
