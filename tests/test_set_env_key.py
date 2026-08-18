"""tools/set_env_key.py — the tool that writes secrets into env files.

It exists so a key never reaches a screen, a shell history, or an agent
transcript, and so rotating one secret cannot destroy the others sitting beside
it in a gitignored file with no backup.
"""

import importlib.util
import os
import stat
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "set_env_key",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "set_env_key.py"),
)
set_env_key = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(set_env_key)


@pytest.fixture
def envfile(tmp_path):
    p = tmp_path / "dotenv"
    p.write_text("FOO=1\nOPENROUTER_API_KEY=old-value\n# a comment about CEREBRAS_PAID_API_KEY\nCEREBRAS_PAID_API_KEY=sibling-secret\n")
    return p


def _run(monkeypatch, envfile, name, value, tty=True):
    monkeypatch.setattr(sys, "argv", ["set_env_key.py", "--file", str(envfile), name])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(set_env_key.getpass, "getpass", lambda prompt="": value)
    return set_env_key.main()


def test_replaces_only_the_named_assignment(monkeypatch, envfile):
    assert _run(monkeypatch, envfile, "OPENROUTER_API_KEY", "new-value") == 0
    lines = envfile.read_text().splitlines()
    assert "OPENROUTER_API_KEY=new-value" in lines
    assert "CEREBRAS_PAID_API_KEY=sibling-secret" in lines, "the neighbouring secret must survive"
    assert "FOO=1" in lines
    assert "# a comment about CEREBRAS_PAID_API_KEY" in lines, "a comment is not an assignment"


def test_appends_when_the_variable_is_absent(monkeypatch, envfile):
    assert _run(monkeypatch, envfile, "NEW_KEY", "v") == 0
    assert "NEW_KEY=v" in envfile.read_text().splitlines()


def test_file_ends_up_private(monkeypatch, envfile):
    envfile.chmod(0o644)
    _run(monkeypatch, envfile, "OPENROUTER_API_KEY", "new-value")
    assert stat.S_IMODE(envfile.stat().st_mode) == 0o600


def test_a_new_file_is_never_briefly_world_readable(monkeypatch, tmp_path):
    """The temp file is opened 0600, so the secret is never in a 0644 file."""
    target = tmp_path / "fresh.env"
    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **kw):
        if str(path).startswith(str(target)):
            modes.append(mode)
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    assert _run(monkeypatch, target, "OPENROUTER_API_KEY", "v") == 0
    assert modes and all(m == 0o600 for m in modes), modes
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_refuses_without_a_terminal_and_leaves_the_file_alone(monkeypatch, envfile, capsys):
    before = envfile.read_text()
    assert _run(monkeypatch, envfile, "OPENROUTER_API_KEY", "new-value", tty=False) == 2
    assert envfile.read_text() == before
    assert "no terminal available" in capsys.readouterr().err


def test_a_failed_write_leaves_the_original_intact(monkeypatch, envfile):
    """The old code truncated the real file first: an interrupt mid-write took
    every other secret with it."""
    before = envfile.read_text()

    class Boom(Exception):
        pass

    real_fdopen = os.fdopen

    def exploding_fdopen(fd, *a, **kw):
        fh = real_fdopen(fd, *a, **kw)
        orig_write = fh.write

        def write(s):
            orig_write(s[: len(s) // 2])
            raise Boom("disk full")

        fh.write = write
        return fh

    monkeypatch.setattr(os, "fdopen", exploding_fdopen)
    with pytest.raises(Boom):
        _run(monkeypatch, envfile, "OPENROUTER_API_KEY", "new-value")
    assert envfile.read_text() == before
    assert not list(envfile.parent.glob("*.tmp")), "no temp file left behind"


def test_says_nothing_about_the_value(monkeypatch, envfile, capsys):
    _run(monkeypatch, envfile, "OPENROUTER_API_KEY", "sk-or-v1-supersecret")
    out = capsys.readouterr()
    assert "supersecret" not in out.out + out.err
    assert "cret" not in out.out, "not even a fingerprint of the value"
    assert str(envfile) in out.out


def test_refuses_a_bogus_variable_name(monkeypatch, envfile):
    before = envfile.read_text()
    assert _run(monkeypatch, envfile, "BAD NAME; rm -rf /", "v") == 2
    assert envfile.read_text() == before
