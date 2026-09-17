#!/usr/bin/env python3
"""How commands are wrapped, and what environment a local one inherits."""

import os
import subprocess

import pytest

from aspects.ssh_executor import LocalExecutor, SSHExecutor, build_executor


def wrapper(username="root", sudo_available=True):
    """An SSHExecutor with no transport, for testing command construction."""
    executor = SSHExecutor.__new__(SSHExecutor)
    executor.username = username
    executor._sudo_prefix = None
    executor._sudo_user_prefix = None
    executor.which = lambda binary: "/usr/bin/sudo" if sudo_available else None
    return executor


def test_root_runs_privileged_commands_directly():
    assert wrapper().  _wrap("systemctl status x", "root") == \
        "bash -c 'systemctl status x'"


def test_an_unprivileged_login_uses_sudo():
    assert wrapper(username="rocky")._wrap("systemctl status x", "root") == \
        "sudo -n bash -c 'systemctl status x'"


def test_stepping_down_to_another_user_always_invokes_sudo():
    """sudo_prefix() is empty as root — correct for privileged commands, but
    it would leave a bare `-u postgres` for the shell to choke on."""
    assert wrapper()._wrap("psql -c 'select 1'", "postgres").startswith(
        "sudo -n -u postgres -- bash -c ")


def test_stepping_down_without_sudo_uses_runuser():
    assert wrapper(sudo_available=False)._wrap("psql", "postgres").startswith(
        "runuser -u postgres -- bash -c ")


def test_a_command_as_the_login_user_is_not_wrapped():
    assert wrapper(username="rocky")._wrap("whoami", "rocky") == "bash -c whoami"


def test_commands_are_quoted_as_one_argument():
    wrapped = wrapper()._wrap("echo 'a b'; rm -rf /", "postgres")

    assert wrapped.count("bash -c ") == 1
    assert wrapped.endswith("'echo '\"'\"'a b'\"'\"'; rm -rf /'")


# ---------------------------------------------------------------------------
# the local execution environment
# ---------------------------------------------------------------------------


def test_host_environment_drops_this_tools_virtualenv(monkeypatch):
    """A venv built by an inherited `python3` points back into the checkout,
    where the postgres user cannot follow."""
    monkeypatch.setenv("VIRTUAL_ENV", "/root/postgres-cluster-deployment/venv")
    monkeypatch.setenv("PATH", "/root/postgres-cluster-deployment/venv/bin:/usr/bin:/bin")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("PYTHONHOME", "/elsewhere")

    env = LocalExecutor.host_environment()

    assert "VIRTUAL_ENV" not in env
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert env["PATH"] == "/usr/bin:/bin"


def test_host_environment_is_untouched_outside_a_virtualenv(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert LocalExecutor.host_environment()["PATH"] == "/usr/bin:/bin"


def test_a_local_command_really_runs_without_the_venv(monkeypatch, tmp_path):
    """End to end through subprocess, not just the environment dict."""
    fake_venv = tmp_path / "venv"
    (fake_venv / "bin").mkdir(parents=True)
    (fake_venv / "bin" / "python3").write_text("#!/bin/sh\necho fake\n")
    (fake_venv / "bin" / "python3").chmod(0o755)
    monkeypatch.setenv("VIRTUAL_ENV", str(fake_venv))
    monkeypatch.setenv("PATH", f"{fake_venv}/bin:{os.environ['PATH']}")

    code, output = LocalExecutor(name="local").exec_run(
        "command -v python3; echo VIRTUAL_ENV=${VIRTUAL_ENV:-unset}", user=None)

    assert code == 0
    assert str(fake_venv) not in output
    assert "VIRTUAL_ENV=unset" in output


def test_local_executor_reports_failures_without_raising():
    ok, _ = LocalExecutor(name="local").try_run("exit 3", user=None)

    assert ok is False


# ---------------------------------------------------------------------------
# choosing an executor
# ---------------------------------------------------------------------------


def test_local_flag_skips_ssh():
    executor = build_executor({"name": "local", "host": "localhost", "local": True})

    assert isinstance(executor, LocalExecutor)


def test_localhost_without_the_flag_still_goes_over_ssh():
    """Deploying to this machine and to a loopback SSH endpoint differ."""
    executor = build_executor({"name": "lo", "host": "localhost",
                               "username": "rocky"})

    assert isinstance(executor, SSHExecutor)
    assert not isinstance(executor, LocalExecutor)
    assert executor.username == "rocky"
