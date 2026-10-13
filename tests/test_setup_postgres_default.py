"""Tests for the Postgres-by-default model in setup-dispatch-host.sh.

Step 4.5c (Postgres + pgvector) runs unless --without-postgres is passed
(--with-postgres is a no-op alias), --without-postgres strips the Postgres env
keys, and Step 4.5d verifies — fail closed — any host whose service env
requires Postgres tests.

The REAL script is run with bash. The full installer needs root, apt and
systemd, so the script's SETUP_TEST_PG_ONLY seam executes just the Postgres
steps, extracted from the script file itself. External commands are shims on a
REPLACED PATH (a directory holding only symlinks to the few real tools the
script needs plus the shims) — never a prepended PATH, so nothing real leaks.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SETUP_SCRIPT = Path(__file__).parent.parent / "setup-dispatch-host.sh"

_REAL_TOOLS = [
    "bash",
    "cat",
    "cut",
    "dirname",
    "grep",
    "head",
    "id",
    "install",
    "mktemp",
    "python3",
    "rm",
    "sed",
    "seq",
    "sort",
    "tail",
    "tr",
]

_PSQL_SHIM = """#!/bin/bash
# Logs every call; PSHIM_MODE: ok | no_connect | no_vector
echo "psql $*" >> "$PSHIM_LOG"
if [[ ! -t 0 ]]; then stdin="$(cat)"; else stdin=""; fi
[[ -n "$stdin" ]] && echo "stdin: $stdin" >> "$PSHIM_LOG"
case "$*" in *pg_roles*) echo 1; exit 0 ;; esac
[[ -n "$PSHIM_SUPER" ]] && exit 0
case "$PSHIM_MODE" in
  no_connect) echo 'psql: error: connection to server failed' >&2; exit 2 ;;
esac
if [[ -n "$stdin" ]]; then
  if [[ "$PSHIM_MODE" == "no_vector" ]]; then echo 'ERROR: type "vector" does not exist' >&2; exit 3; fi
  echo "0.8.0,1"
fi
exit 0
"""


@pytest.fixture
def host(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in _REAL_TOOLS:
        real = shutil.which(tool)
        assert real, tool
        (bindir / tool).symlink_to(real)
    libdir = tmp_path / "pglib"
    libdir.mkdir()
    (libdir / "vector.so").write_text("")

    def shim(name: str, body: str) -> None:
        f = bindir / name
        f.write_text("#!/bin/bash\n" + body)
        f.chmod(0o755)

    shim("apt-get", 'echo apt-get "$@" >> "$PSHIM_LOG"\n')
    shim("dpkg-query", "printf 'install ok installed'\n")
    shim("systemctl", "exit 0\n")
    shim(
        "pg_config",
        f'[[ "$1" == --pkglibdir ]] && echo {libdir} || echo "PostgreSQL 16.1"\n',
    )
    shim("sudo", 'shift 2; PSHIM_SUPER=1 exec "$@"\n')
    shim("runuser", 'echo "runuser $*" >> "$PSHIM_LOG"; shift 3; exec "$@"\n')
    (bindir / "psql").write_text(_PSQL_SHIM)
    (bindir / "psql").chmod(0o755)

    env_file = tmp_path / "service.env"
    log = tmp_path / "shim.log"
    log.write_text("")

    def run(*flags: str, mode: str = "ok", env_text: str | None = None):
        if env_text is not None:
            env_file.write_text(env_text)
        env = {
            "PATH": str(bindir),
            "PSHIM_LOG": str(log),
            "PSHIM_MODE": mode,
            "SETUP_TEST_PG_ONLY": "1",
            # Required by the script's early config check; never contacted here.
            "DISPATCH_WHEEL_INDEX": "https://pypi.example.com/simple/",
            "SETUP_TEST_SERVICE_ENV": str(env_file),
            "SERVICE_USER_PLACEHOLDER": "x",
        }
        return subprocess.run(
            [
                str(bindir / "bash"),
                str(SETUP_SCRIPT),
                "--agent-user",
                "agentx",
                "--service-user",
                # Service user must be a REAL user on this host (install -o/-g
                # below needs its uid/primary group). USER is not exported in
                # every environment this suite runs in, so fall back through
                # getpass.getuser()'s LOGNAME/pwd chain rather than hardcoding
                # "nobody" — whose matching *group* does not exist on every
                # host (Debian calls it "nogroup"), which made install -g fail.
                os.environ.get("USER") or getpass.getuser(),
                *flags,
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    run.env_file = env_file  # type: ignore[attr-defined]
    run.log = log  # type: ignore[attr-defined]
    return run


_BASE_ENV = "FOO=bar\nTOKEN_X=sekret-value-123\n"
_PG_ENV = (
    _BASE_ENV
    + "KB_TEST_DATABASE_URL=postgresql:///postgres\nKB_REQUIRE_POSTGRES_TESTS=1\n"
)


class TestFlagsAndDefaults:
    def test_default_runs_step_4_5c_and_writes_keys(self, host) -> None:
        r = host(env_text=_BASE_ENV)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Step 4.5c" in r.stdout and "[4.5c-D]" in r.stdout
        text = host.env_file.read_text()
        assert "KB_TEST_DATABASE_URL=postgresql:///postgres\n" in text
        assert "KB_REQUIRE_POSTGRES_TESTS=1\n" in text
        assert "FOO=bar" in text

    def test_with_postgres_alias_is_accepted_and_equivalent(self, host) -> None:
        r = host("--with-postgres", env_text=_BASE_ENV)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Unknown option" not in r.stderr
        assert "KB_REQUIRE_POSTGRES_TESTS=1\n" in host.env_file.read_text()

    def test_without_postgres_skips_and_strips_keys(self, host) -> None:
        r = host("--without-postgres", env_text=_PG_ENV)
        assert r.returncode == 0, r.stdout + r.stderr
        text = host.env_file.read_text()
        assert "KB_TEST_DATABASE_URL" not in text
        assert "KB_REQUIRE_POSTGRES_TESTS" not in text
        assert "FOO=bar" in text and "TOKEN_X=sekret-value-123" in text
        assert "Removed KB_TEST_DATABASE_URL and KB_REQUIRE_POSTGRES_TESTS" in r.stdout
        assert "[4.5c-D]" not in r.stdout
        assert "verification skipped" in r.stdout
        assert "psql" not in host.log.read_text()

    def test_without_postgres_with_no_keys_is_a_clean_noop(self, host) -> None:
        r = host("--without-postgres", env_text=_BASE_ENV)
        assert r.returncode == 0, r.stdout + r.stderr
        assert host.env_file.read_text() == _BASE_ENV

    def test_dry_run_prints_verification_and_mutates_nothing(self, host) -> None:
        r = host("--dry-run", env_text=_BASE_ENV)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Would: verify Postgres as agentx" in r.stdout
        assert host.env_file.read_text() == _BASE_ENV
        assert host.log.read_text() == ""

    def test_dry_run_without_postgres_does_not_strip(self, host) -> None:
        r = host("--without-postgres", "--dry-run", env_text=_PG_ENV)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Would: remove KB_TEST_DATABASE_URL" in r.stdout
        assert host.env_file.read_text() == _PG_ENV

    def test_usage_documents_default_on(self) -> None:
        r = subprocess.run(
            [str(SETUP_SCRIPT), "--help"], capture_output=True, text=True
        )
        assert "--without-postgres" in r.stdout
        assert "--with-postgres" in r.stdout
        assert "no-op alias" in r.stdout
        assert "opt-in; default off" not in r.stdout.split("--with-postgres")[0][-400:]


class TestVerification:
    def test_pass_runs_without_smoke(self, host) -> None:
        r = host(env_text=_PG_ENV)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Postgres verified" in r.stdout
        log = host.log.read_text()
        assert "runuser -u agentx -- psql postgresql:///postgres" in log
        assert "CREATE DATABASE kb_smoke_" in log
        assert "DROP DATABASE IF EXISTS kb_smoke_" in log

    def test_verifies_pre_existing_env_even_with_no_install_work(self, host) -> None:
        # Host already has the keys (e.g. an already-provisioned host): verification runs regardless.
        r = host(env_text=_PG_ENV)
        assert "Step 4.5d" in r.stdout

    def test_fail_closed_when_connection_fails(self, host) -> None:
        r = host(mode="no_connect", env_text=_PG_ENV)
        assert r.returncode != 0
        err = r.stderr
        assert "Postgres verification FAILED" in err
        assert str(host.env_file) in err
        assert "KB_TEST_DATABASE_URL" in err and "KB_REQUIRE_POSTGRES_TESTS" in err
        assert "re-run setup-dispatch-host.sh" in err
        assert "--without-postgres" in err
        assert "sekret-value-123" not in r.stdout + err

    def test_fail_closed_when_vector_missing(self, host) -> None:
        r = host(mode="no_vector", env_text=_PG_ENV)
        assert r.returncode != 0
        assert "Postgres verification FAILED" in r.stderr
        assert "DROP DATABASE IF EXISTS kb_smoke_" in host.log.read_text()

    def test_without_postgres_never_verifies(self, host) -> None:
        r = host("--without-postgres", mode="no_connect", env_text=_PG_ENV)
        assert r.returncode == 0, r.stdout + r.stderr
