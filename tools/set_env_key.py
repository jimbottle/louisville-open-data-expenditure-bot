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

    # Fail closed. Without a controlling terminal getpass falls back to reading
    # stdin WITH ECHO ON — on the documented server path (driven through an MCP
    # runner, not a tty) that would print the secret into the captured output,
    # the one thing this script exists to prevent. A stderr warning is not
    # enough when the damage is "rotate the key again".
    if not sys.stdin.isatty():
        print("no terminal available: this would echo the secret. Run it in an "
              "interactive shell (ssh to the host if need be).", file=sys.stderr)
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

    # Write a private temp file and rename over the target. Truncating the real
    # file first meant an interrupt (Ctrl-C, full disk, encoding error) between
    # truncation and the finished write destroyed every OTHER secret in it —
    # and .env is gitignored, so there is nothing to recover from. Creating the
    # temp file with 0600 from the start also closes the window where a
    # newly-created env file sits at 0644 with a secret already in it.
    tmp = args.file + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, args.file)  # atomic within one filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(args.file, 0o600)

    # No length, no last-four, no digest: a fingerprint is still key material,
    # and any of it on a screen or in a log is a reason to rotate again.
    print(f"{'replaced' if replaced else 'appended'} {args.name} in {args.file}; mode 600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
