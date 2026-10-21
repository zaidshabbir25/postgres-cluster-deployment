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


# ---------------------------------------------------------------------------
# loading SSH keys across paramiko versions
# ---------------------------------------------------------------------------


def test_only_key_types_this_paramiko_has_are_tried():
    """paramiko 4 removed DSSKey. Naming it in a literal tuple raises
    AttributeError while the tuple is built — before a single key is tried —
    so an Ed25519 key that would load fine failed with "module 'paramiko' has
    no attribute 'DSSKey'"."""
    import paramiko

    from aspects.ssh_executor import SSHExecutor

    classes = SSHExecutor._key_classes()

    assert classes, "no key types at all"
    assert all(getattr(paramiko, c.__name__, None) is c for c in classes)
    if not hasattr(paramiko, "DSSKey"):
        assert "DSSKey" not in [c.__name__ for c in classes]


def test_a_missing_key_type_does_not_stop_the_others(monkeypatch):
    """The failure mode this guards against: one absent attribute taking out
    every key type, including the ones that are present."""
    import paramiko

    from aspects.ssh_executor import SSHExecutor

    monkeypatch.delattr(paramiko, "ECDSAKey", raising=False)

    names = [c.__name__ for c in SSHExecutor._key_classes()]

    assert "ECDSAKey" not in names
    assert "Ed25519Key" in names and "RSAKey" in names


@pytest.mark.parametrize("key_type", ["ed25519", "rsa", "ecdsa"])
def test_a_real_key_of_each_supported_type_loads(tmp_path, key_type):
    """Generated here rather than fixtured, so this follows whatever the
    installed OpenSSH and paramiko agree on."""
    import shutil
    import subprocess

    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is required to make a key to load")

    from aspects.ssh_executor import SSHExecutor

    path = tmp_path / f"id_{key_type}"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", key_type, "-N", "", "-f", str(path)],
        check=True, capture_output=True,
    )

    key = SSHExecutor._load_key(path)

    assert key.get_name()


def test_an_unreadable_key_says_what_each_type_complained_about(tmp_path):
    from aspects.ssh_executor import SSHExecutor

    path = tmp_path / "notakey"
    path.write_text("this is not a private key")

    with pytest.raises(ValueError, match="Unsupported or encrypted SSH key"):
        SSHExecutor._load_key(path)


# ---------------------------------------------------------------------------
# keys with a passphrase
# ---------------------------------------------------------------------------


def make_key(tmp_path, passphrase=""):
    """A real key, so these test what paramiko actually does with one."""
    import shutil
    import subprocess

    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is required to make a key")

    path = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(path)],
        check=True, capture_output=True,
    )
    return path


def test_an_encrypted_key_is_distinguished_from_an_unreadable_one(tmp_path):
    """"Needs a passphrase" and "not a key I understand" lead somewhere
    different, so the caller has to be able to tell them apart."""
    import paramiko

    from aspects.ssh_executor import SSHExecutor

    path = make_key(tmp_path, passphrase="hunter2")

    with pytest.raises(paramiko.PasswordRequiredException):
        SSHExecutor._load_key(path)


def test_an_encrypted_key_loads_when_the_passphrase_is_given(tmp_path):
    from aspects.ssh_executor import SSHExecutor

    path = make_key(tmp_path, passphrase="hunter2")

    assert SSHExecutor._load_key(path, "hunter2").get_name()


def test_the_passphrase_comes_from_the_environment_for_an_unattended_run(
        monkeypatch):
    from aspects.ssh_executor import SSHExecutor

    monkeypatch.setenv("PG_CLUSTER_SSH_PASSPHRASE", "from-env")

    assert SSHExecutor.passphrase_for("/any/key") == "from-env"


def test_a_scripted_run_is_never_prompted(monkeypatch):
    """A prompt with no terminal behind it would hang the run; falling back to
    the agent is the useful thing to do instead."""
    from aspects import ssh_executor
    from aspects.ssh_executor import SSHExecutor

    monkeypatch.delenv("PG_CLUSTER_SSH_PASSPHRASE", raising=False)
    monkeypatch.setattr(ssh_executor.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(ssh_executor, "_PASSPHRASES", {})
    monkeypatch.setattr(ssh_executor.getpass, "getpass",
                        lambda *a, **k: pytest.fail("should not have prompted"))

    assert SSHExecutor.passphrase_for("/any/key") is None


def test_one_key_is_asked_about_once_however_many_hosts_use_it(monkeypatch):
    """A twenty-host inventory shares one key far more often than not."""
    from aspects import ssh_executor
    from aspects.ssh_executor import SSHExecutor

    monkeypatch.delenv("PG_CLUSTER_SSH_PASSPHRASE", raising=False)
    monkeypatch.setattr(ssh_executor.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ssh_executor, "_PASSPHRASES", {})
    asked = []

    def prompt(message=""):
        asked.append(message)
        return "secret"

    monkeypatch.setattr(ssh_executor.getpass, "getpass", prompt)

    first = SSHExecutor.passphrase_for("/same/key")
    second = SSHExecutor.passphrase_for("/same/key")

    assert (first, second) == ("secret", "secret")
    assert len(asked) == 1


def test_declining_the_prompt_is_remembered_too(monkeypatch):
    """Pressing Enter means "use the agent", and asking again every host would
    make that answer unusable."""
    from aspects import ssh_executor
    from aspects.ssh_executor import SSHExecutor

    monkeypatch.delenv("PG_CLUSTER_SSH_PASSPHRASE", raising=False)
    monkeypatch.setattr(ssh_executor.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ssh_executor, "_PASSPHRASES", {})
    asked = []
    monkeypatch.setattr(ssh_executor.getpass, "getpass",
                        lambda *a, **k: asked.append(1) or "")

    assert SSHExecutor.passphrase_for("/k") is None
    assert SSHExecutor.passphrase_for("/k") is None
    assert len(asked) == 1


def test_an_encrypted_key_with_no_passphrase_falls_back_to_the_agent(
        tmp_path, monkeypatch):
    """A key with a passphrase is usually loaded into an agent already, which
    is the whole reason for having one."""
    from aspects import ssh_executor
    from aspects.ssh_executor import SSHExecutor

    monkeypatch.delenv("PG_CLUSTER_SSH_PASSPHRASE", raising=False)
    monkeypatch.setattr(ssh_executor.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(ssh_executor, "_PASSPHRASES", {})
    path = make_key(tmp_path, passphrase="hunter2")
    seen = {}

    class FakeClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            seen.update(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", FakeClient)

    SSHExecutor(name="h", host="10.0.0.1", username="rocky",
                key_file=str(path)).connect()

    assert seen["pkey"] is None, "no key was loaded"
    assert seen["allow_agent"] is True and seen["look_for_keys"] is True


def test_an_encrypted_key_the_agent_cannot_unlock_is_not_retried(
        tmp_path, monkeypatch):
    """Retrying cannot help — the key is just as encrypted next time — and
    three identical failures only delay the message."""
    import paramiko

    from aspects import ssh_executor
    from aspects.ssh_executor import SSHExecutor

    monkeypatch.delenv("PG_CLUSTER_SSH_PASSPHRASE", raising=False)
    monkeypatch.setattr(ssh_executor.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(ssh_executor, "_PASSPHRASES", {})
    path = make_key(tmp_path, passphrase="hunter2")
    attempts = []

    class FakeClient:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            attempts.append(1)
            raise paramiko.PasswordRequiredException("encrypted")

        def close(self):
            pass

    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", FakeClient)

    with pytest.raises(ConnectionError) as raised:
        SSHExecutor(name="h", host="10.0.0.1", username="rocky",
                    key_file=str(path)).connect()

    assert len(attempts) == 1, "it should not have retried"
    message = str(raised.value)
    assert "PG_CLUSTER_SSH_PASSPHRASE" in message
    assert "ssh-add" in message


def test_a_missing_key_says_where_to_point_it(tmp_path):
    from aspects.ssh_executor import SSHExecutor

    with pytest.raises(FileNotFoundError, match="PG_CLUSTER_SSH_KEY"):
        SSHExecutor(name="h", host="10.0.0.1", username="rocky",
                    key_file=str(tmp_path / "absent")).connect()
