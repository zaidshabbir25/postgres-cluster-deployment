#!/usr/bin/env python3
"""SSH command executor — the single channel every aspect uses to touch a host.

One executor per host, shared by all nodes placed on that host. Every command
is wrapped in `sudo bash -c '<cmd>'` so compound shell (pipes, redirects, &&)
survives intact — the reference framework's bare `sudo <cmd>` form silently
drops everything after the first pipe.

Connections are lazy, cached and self-healing: a dropped transport is
reconnected on the next command rather than failing the deployment.
"""

import io
import getpass
import os
import sys
import shlex
import socket
import tarfile
import time
from pathlib import Path

try:
    import paramiko
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "paramiko is required. Install dependencies with:\n"
        "  python3 -m venv venv && source venv/bin/activate\n"
        "  pip install -r requirements.txt"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TIMEOUT = 600


class CommandFailed(RuntimeError):
    """A remote command returned a non-zero exit code."""

    def __init__(self, host, command, exit_code, output):
        self.host = host
        self.command = command
        self.exit_code = exit_code
        self.output = output
        super().__init__(
            f"[{host}] command failed (exit {exit_code}): {command}\n{output.strip()}"
        )


#: Passphrases already asked for, by key path. A twenty-host inventory shares
#: one key far more often than not, and asking per host would be unusable.
_UNASKED = object()
_PASSPHRASES = {}


class SSHExecutor:
    """A connected host. Commands run through sudo unless a user is given."""

    def __init__(self, name, host, username, key_file=None, port=22,
                 run_logger=None, max_retries=3):
        self.name = name
        self.host = host
        self.username = username
        self.port = int(port)
        # Resolution order: inventory key_file -> PG_CLUSTER_SSH_KEY -> ssh agent
        self.key_file = key_file or os.environ.get("PG_CLUSTER_SSH_KEY") or None
        self._log = run_logger
        self._max_retries = max_retries
        self._client = None
        self._sudo_prefix = None
        self._sudo_user_prefix = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        """Open the SSH connection, retrying transient failures."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        pkey = None
        use_agent = True
        if self.key_file:
            key_path = Path(self.key_file)
            if not key_path.is_absolute():
                key_path = REPO_ROOT / key_path
            if not key_path.exists():
                raise FileNotFoundError(
                    f"SSH key not found: {key_path}. Set PG_CLUSTER_SSH_KEY or "
                    f"fix key_file in the inventory."
                )
            try:
                pkey = self._load_key(key_path)
            except paramiko.PasswordRequiredException:
                # Encrypted. Ask for the passphrase, or let the agent try: a
                # key with a passphrase is usually loaded into one already,
                # which is the whole reason for having the agent.
                passphrase = self.passphrase_for(key_path)
                if passphrase:
                    pkey = self._load_key(key_path, passphrase)
                else:
                    self._emit(
                        f"{key_path} is encrypted and no passphrase was given "
                        f"— trying the ssh agent instead. Set "
                        f"PG_CLUSTER_SSH_PASSPHRASE to use the key directly."
                    )
            use_agent = pkey is None

        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    pkey=pkey,
                    timeout=30,
                    banner_timeout=45,
                    auth_timeout=45,
                    look_for_keys=use_agent,
                    allow_agent=use_agent,
                )
                self._client = client
                self._emit(f"connected to {self.username}@{self.host}:{self.port}")
                return self
            except paramiko.PasswordRequiredException as exc:
                # Retrying cannot help: the key will be just as encrypted next
                # time, and three identical failures only delay the message.
                if self.key_file:
                    which = f"{key_path} is encrypted"
                    add = f"ssh-add {self.key_file}"
                else:
                    # Nothing configured, so paramiko was scanning ~/.ssh and
                    # found something encrypted there. Naming no key at all
                    # leaves the reader with nowhere to start.
                    which = ("no key is configured for this host, and an "
                             "encrypted one was found in ~/.ssh")
                    add = "ssh-add"
                raise ConnectionError(
                    f"Cannot SSH to {self.username}@{self.host}:{self.port}: "
                    f"{which}, and neither a passphrase nor the ssh agent "
                    f"could unlock it.\n"
                    f"  Add it to your agent:      {add}\n"
                    f"  Or give the passphrase:    "
                    f"export PG_CLUSTER_SSH_PASSPHRASE=...\n"
                    f"  Or name the key to use:    "
                    f"export PG_CLUSTER_SSH_KEY=/path/to/key\n"
                    f"  Or set key_file for this host in the inventory."
                ) from exc
            except Exception as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    self._emit(f"connect attempt {attempt} failed ({exc}) — retrying")
                    time.sleep(3)

        raise ConnectionError(
            f"Cannot SSH to {self.username}@{self.host}:{self.port} "
            f"after {self._max_retries} attempts: {last_exc}"
        )

    @staticmethod
    def passphrase_for(key_path):
        """The passphrase for an encrypted key, or None.

        Looked up once per process and cached: a twenty-host inventory would
        otherwise ask twenty times for the same key. Sources, in order:

          * PG_CLUSTER_SSH_PASSPHRASE, for an unattended run
          * a prompt, when there is a terminal to ask at

        No prompt without a terminal: a scripted run would hang on it, and the
        caller can fall back to the agent instead.
        """
        from_env = os.environ.get("PG_CLUSTER_SSH_PASSPHRASE")
        if from_env:
            return from_env

        cached = _PASSPHRASES.get(str(key_path), _UNASKED)
        if cached is not _UNASKED:
            return cached

        answer = None
        if sys.stdin.isatty():
            try:
                answer = getpass.getpass(
                    f"Passphrase for {key_path} (Enter to try the ssh agent): "
                ) or None
            except (EOFError, KeyboardInterrupt):
                answer = None
        _PASSPHRASES[str(key_path)] = answer
        return answer

    @staticmethod
    def _key_classes():
        """The key types this paramiko can load, newest algorithm first.

        Looked up rather than listed: paramiko 4 removed DSSKey, and naming it
        in a literal tuple raises AttributeError while the tuple is being
        built — before a single key has been tried, so an Ed25519 key that
        would have loaded fine fails with "module 'paramiko' has no attribute
        'DSSKey'". DSA has been off by default in OpenSSH for years, so its
        absence is a reason to stop naming it, not to pin paramiko back.
        """
        names = ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey")
        return [getattr(paramiko, name) for name in names
                if hasattr(paramiko, name)]

    @staticmethod
    def _load_key(key_path, passphrase=None):
        """Load a private key without needing to know its algorithm up front.

        Raises paramiko.PasswordRequiredException when the key is encrypted and
        no passphrase was given, so the caller can tell "needs a passphrase"
        from "not a key I understand" — they lead somewhere different.
        """
        # paramiko 4 added a loader that detects the type itself. Preferred
        # where it exists: it understands formats the per-class loaders do not.
        loader = getattr(paramiko.PKey, "from_path", None)
        if loader is not None:
            try:
                return loader(str(key_path), passphrase)
            except paramiko.PasswordRequiredException:
                raise
            except Exception:
                pass  # fall through to trying each class, for a better message

        errors = []
        encrypted = False
        for key_class in SSHExecutor._key_classes():
            try:
                return key_class.from_private_key_file(str(key_path), passphrase)
            except paramiko.PasswordRequiredException:
                encrypted = True
            except Exception as exc:
                errors.append(f"{key_class.__name__}: {exc}")
        if encrypted:
            raise paramiko.PasswordRequiredException(
                f"{key_path} is encrypted"
            )
        raise ValueError(
            f"Unsupported or encrypted SSH key {key_path}:\n  " + "\n  ".join(errors)
        )

    def _connected(self):
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def _ensure_connected(self):
        if not self._connected():
            self.connect()

    def close(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __enter__(self):
        self._ensure_connected()
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _wrap(self, command, user):
        """Build the privileged command line for a target user."""
        quoted = shlex.quote(command)
        if user is None or user == self.username:
            return f"bash -c {quoted}"
        if user == "root":
            return f"{self.sudo_prefix()} bash -c {quoted}"
        # Stepping *down* to another user always needs its own invocation:
        # sudo_prefix() is empty in a root session, which is right for a plain
        # privileged command but would leave a bare "-u postgres bash -c ..."
        # for the shell to choke on.
        return (f"{self._step_down_prefix()} -u {shlex.quote(user)} -- "
                f"bash -c {quoted}")

    def _step_down_prefix(self):
        """`sudo -n`, or runuser where sudo is not installed."""
        if self._sudo_user_prefix is None:
            prefix = self.sudo_prefix()
            if not prefix:
                prefix = "sudo -n" if self.which("sudo") else "runuser"
            self._sudo_user_prefix = prefix
        return self._sudo_user_prefix

    def sudo_prefix(self):
        """Non-interactive sudo, or nothing when we are already root.

        Public because callers that build their own command lines (launching a
        daemon with nohup, piping into tee) need the same privilege prefix.
        """
        if self._sudo_prefix is None:
            self._sudo_prefix = "sudo -n" if self.username != "root" else ""
        return self._sudo_prefix

    def exec_run(self, command, user="root", timeout=DEFAULT_TIMEOUT, node=None):
        """Run a command. Returns (exit_code, output) with stderr folded in."""
        self._ensure_connected()
        full = self._wrap(command, user).strip()

        started = time.time()
        for attempt in range(1, self._max_retries + 1):
            try:
                _, stdout, stderr = self._client.exec_command(full, timeout=timeout)
                stdout.channel.settimeout(timeout)
                # Drain stdout before recv_exit_status(): the channel only closes
                # once output is flushed, so asking for the status first deadlocks
                # on any command that fills the pipe buffer.
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                exit_code = stdout.channel.recv_exit_status()
                output = out + err
                self._record(node, full, exit_code, output, time.time() - started)
                return exit_code, output
            except socket.timeout as exc:
                raise TimeoutError(
                    f"[{self.host}] timed out after {timeout}s: {command}"
                ) from exc
            except (paramiko.SSHException, EOFError, OSError) as exc:
                if attempt < self._max_retries:
                    self._emit(f"command transport error ({exc}) — reconnecting")
                    self.connect()
                    time.sleep(2)
                else:
                    raise

    def run(self, command, user="root", timeout=DEFAULT_TIMEOUT, node=None, message=""):
        """Run a command and raise CommandFailed on a non-zero exit."""
        exit_code, output = self.exec_run(command, user=user, timeout=timeout, node=node)
        if exit_code != 0:
            raise CommandFailed(self.host, message or command, exit_code, output)
        return output

    def try_run(self, command, user="root", timeout=DEFAULT_TIMEOUT, node=None):
        """Run a command, tolerating failure. Returns (ok, output)."""
        exit_code, output = self.exec_run(command, user=user, timeout=timeout, node=node)
        return exit_code == 0, output

    def exists(self, path):
        ok, _ = self.try_run(f"test -e {shlex.quote(path)}")
        return ok

    def which(self, binary):
        ok, output = self.try_run(f"command -v {shlex.quote(binary)}")
        return output.strip() if ok else None

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    def write_file(self, remote_path, content, owner=None, mode=None, node=None):
        """Write text to a remote path via `sudo tee`.

        tee avoids two problems at once: SFTP cannot write to root-owned
        directories as an unprivileged login user, and a heredoc large enough
        to matter resets paramiko's exec channel.
        """
        self._ensure_connected()
        parent = os.path.dirname(remote_path.rstrip("/")) or "/"
        self.run(f"mkdir -p {shlex.quote(parent)}", node=node)

        cmd = f"{self.sudo_prefix()} tee {shlex.quote(remote_path)} > /dev/null".strip()
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=DEFAULT_TIMEOUT)
        stdin.write(content)
        stdin.channel.shutdown_write()
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr.read().decode("utf-8", errors="replace")
            raise IOError(f"[{self.host}] failed writing {remote_path}: {err}")

        if owner:
            self.run(f"chown {owner}:{owner} {shlex.quote(remote_path)}", node=node)
        if mode:
            self.run(f"chmod {mode} {shlex.quote(remote_path)}", node=node)
        self._record(node, f"write {remote_path}", 0, f"{len(content)} bytes", 0.0)

    def put_file(self, local_path, remote_path, owner=None, mode=None, node=None):
        """Upload a local file. Large files go over SFTP into a staging path."""
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(local_path)

        self._ensure_connected()
        staging = f"/tmp/{local_path.name}.upload"
        try:
            sftp = self._client.open_sftp()
            try:
                sftp.put(str(local_path), staging)
            finally:
                sftp.close()
        except (PermissionError, IOError):
            # SFTP unavailable (subsystem disabled or unwritable) — stream it.
            self.write_file(staging, local_path.read_text(encoding="utf-8"), node=node)

        parent = os.path.dirname(remote_path.rstrip("/")) or "/"
        self.run(f"mkdir -p {shlex.quote(parent)}", node=node)
        self.run(f"mv {shlex.quote(staging)} {shlex.quote(remote_path)}", node=node)
        if owner:
            self.run(f"chown {owner}:{owner} {shlex.quote(remote_path)}", node=node)
        if mode:
            self.run(f"chmod {mode} {shlex.quote(remote_path)}", node=node)

    def fetch_text(self, remote_path, user="root"):
        """Read a remote file's contents as text ('' when unreadable)."""
        ok, output = self.try_run(f"cat {shlex.quote(remote_path)}", user=user)
        return output if ok else ""

    def put_archive(self, remote_dir, tarstream):
        """Unpack an in-memory tar into a remote directory (docker-py parity)."""
        self._ensure_connected()
        data = tarstream.read() if hasattr(tarstream, "read") else tarstream
        self.run(f"mkdir -p {shlex.quote(remote_dir)}")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                target = remote_dir.rstrip("/") + "/" + os.path.basename(member.name)
                self.write_file(target, handle.read().decode("utf-8", errors="replace"))

    # ------------------------------------------------------------------
    # Logging plumbing
    # ------------------------------------------------------------------

    def _emit(self, message):
        if self._log:
            self._log.info(f"  [{self.name}] {message}")
        else:
            print(f"  [{self.name}] {message}")

    def _record(self, node, command, exit_code, output, duration):
        if self._log:
            self._log.command(node or self.name, command, exit_code, output, duration)

    def __repr__(self):
        return f"SSHExecutor(name={self.name!r}, host={self.host!r})"


class LocalExecutor(SSHExecutor):
    """Same interface, but runs everything on the machine we are already on.

    Lets a single-host cluster be deployed without SSH round-trips — set a
    host's `"host": "localhost"` and `"local": true` in the inventory.
    """

    def __init__(self, name="localhost", run_logger=None, **_):
        super().__init__(name=name, host="localhost", username=os.getenv("USER", "root"),
                         run_logger=run_logger)

    def connect(self):
        self._emit("using local execution (no SSH)")
        return self

    def _connected(self):
        return True

    def close(self):
        return None

    @staticmethod
    def host_environment():
        """The environment a command on the host should see.

        This process runs inside the deployment tool's own virtualenv, and a
        local command inherits it: `python3` resolves to venv/bin/python3, so
        anything the deployment builds with it is wired back to a directory
        under /root that the postgres user cannot even traverse. An SSH session
        never sees any of this, and neither should a local one.
        """
        import os as _os

        env = dict(_os.environ)
        venv = env.pop("VIRTUAL_ENV", None)
        for variable in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
            env.pop(variable, None)
        if venv:
            venv_bin = _os.path.join(venv, "bin")
            env["PATH"] = ":".join(
                part for part in env.get("PATH", "").split(":")
                if part and _os.path.normpath(part) != _os.path.normpath(venv_bin)
            )
        return env

    def exec_run(self, command, user="root", timeout=DEFAULT_TIMEOUT, node=None):
        import subprocess

        full = self._wrap(command, user).strip()
        started = time.time()
        proc = subprocess.run(
            full, shell=True, capture_output=True, text=True, timeout=timeout,
            env=self.host_environment(),
        )
        output = proc.stdout + proc.stderr
        self._record(node, full, proc.returncode, output, time.time() - started)
        return proc.returncode, output

    def write_file(self, remote_path, content, owner=None, mode=None, node=None):
        import subprocess

        parent = os.path.dirname(remote_path.rstrip("/")) or "/"
        self.run(f"mkdir -p {shlex.quote(parent)}", node=node)
        cmd = f"{self.sudo_prefix()} tee {shlex.quote(remote_path)} > /dev/null".strip()
        proc = subprocess.run(cmd, shell=True, input=content, text=True,
                              capture_output=True, env=self.host_environment())
        if proc.returncode != 0:
            raise IOError(f"failed writing {remote_path}: {proc.stderr}")
        if owner:
            self.run(f"chown {owner}:{owner} {shlex.quote(remote_path)}", node=node)
        if mode:
            self.run(f"chmod {mode} {shlex.quote(remote_path)}", node=node)

    def put_file(self, local_path, remote_path, owner=None, mode=None, node=None):
        self.write_file(remote_path, Path(local_path).read_text(encoding="utf-8"),
                        owner=owner, mode=mode, node=node)


def build_executor(host_config, run_logger=None):
    """Create the right executor for one inventory host entry.

    `"local": true` skips SSH entirely. A host of "localhost" without that flag
    still goes over SSH — deploying to this machine and deploying to a loopback
    SSH endpoint are different setups, and guessing between them would surprise
    whoever wrote the inventory.
    """
    if host_config.get("local"):
        return LocalExecutor(name=host_config.get("name", "localhost"),
                             run_logger=run_logger)
    return SSHExecutor(
        name=host_config.get("name") or host_config["host"],
        host=host_config["host"],
        username=host_config.get("username", "root"),
        key_file=host_config.get("key_file") or None,
        port=host_config.get("port", 22),
        run_logger=run_logger,
    )
