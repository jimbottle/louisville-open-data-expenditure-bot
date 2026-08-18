#!/usr/bin/env python3
"""Set a secret in an env file without it appearing on screen, in shell history,
or in an agent transcript.

    python3 tools/set_env_key.py OPENROUTER_API_KEY
    python3 tools/set_env_key.py --file /Users/macserver/louisville-bot.env OPENROUTER_API_KEY

The value is read with a hidden prompt (getpass), the line is replaced in place
(or appended if absent), the file is chmod 600, and nothing about
the value is printed back.

Exists because editing a dot-prefixed file is awkward in Finder (⌘⇧. toggles
hidden files) and because pasting a key into a chat or a shell command leaks it
into a transcript that outlives the key.
"""
import argparse
import getpass
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="env var to set, e.g. OPENROUTER_API_KEY")
    ap.add_argument("--file", default=".env", help="env file to edit (default: .env)")
    args = ap.parse_args()

    if not args.name.replace("_", "").isalnum():
        print(f"refusing to write a variable named {args.name!r}", file=sys.stderr)
        return 2

    value = getpass.getpass(f"{args.name} (input hidden, paste and press return): ").strip()
    if not value:
        print("nothing entered; file unchanged", file=sys.stderr)
        return 1

    lines = []
    if os.path.exists(args.file):
        with open(args.file) as fh:
            lines = fh.read().splitlines()

    prefix = args.name + "="
    replaced = False
    for i, line in enumerate(lines):
        # Only a real assignment, never a comment that happens to mention the name.
        if line.startswith(prefix):
            lines[i] = prefix + value
            replaced = True
    if not replaced:
        lines.append(prefix + value)

    with open(args.file, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(args.file, 0o600)

    # No length, no last-four, no digest: a fingerprint is still key material,
    # and any of it on a screen or in a log is a reason to rotate again.
    print(f"{'replaced' if replaced else 'appended'} {args.name} in {args.file}; mode 600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
