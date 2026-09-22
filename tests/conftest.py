#!/usr/bin/env python3
"""Shared fixtures: a fake host, and plans to run against it.

Nothing here touches a real machine. Every module under test reaches the
outside world through an executor, so a recording stand-in for that executor is
all it takes to drive the whole deployment logic — which commands were issued,
in what order, and what the code did with the answers.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK  # noqa: E402
from deployment import topology  # noqa: E402


class CommandFailed(RuntimeError):
    """Mirrors ssh_executor.CommandFailed without importing paramiko."""


class FakeExecutor:
    """An executor whose answers are scripted by substring.

    `replies` maps a substring of a command to (ok, output); the first match
    wins, and anything unmatched succeeds with empty output. Every command is
    recorded in `commands`, so tests can assert on what was actually run rather
    than only on return values.
    """

    def __init__(self, replies=None, host="local", username="root",
                 files=("/usr/bin/python3",)):
        self.replies = dict(replies or {})
        self.host = host
        self.username = username
        self.files = set(files)
        self.commands = []
        self.written = {}
        self.uploaded = []

    # --- command execution ------------------------------------------
    def _answer(self, command):
        self.commands.append(command)
        for fragment, reply in self.replies.items():
            if fragment in command:
                return reply
        return True, ""

    def exec_run(self, command, user="root", timeout=None, node=None):
        ok, output = self._answer(command)
        return (0 if ok else 1), output

    def run(self, command, user="root", timeout=None, node=None, message=""):
        ok, output = self._answer(command)
        if not ok:
            raise CommandFailed(message or command)
        return output

    def try_run(self, command, user="root", timeout=None, node=None):
        return self._answer(command)

    # --- inspection helpers -----------------------------------------
    def which(self, binary):
        return f"/usr/bin/{binary}"

    def exists(self, path):
        return path in self.files

    def fetch_text(self, path):
        return self.written.get(path, "")

    def sudo_prefix(self):
        return "" if self.username == "root" else "sudo -n"

    # --- file transfer ----------------------------------------------
    def write_file(self, path, content, owner=None, mode=None, node=None):
        self.written[path] = content

    def put_file(self, local, remote, owner=None, mode=None, node=None):
        self.uploaded.append((str(local), remote))
        self.written[remote] = Path(local).read_text(encoding="utf-8") \
            if Path(local).exists() else ""

    def connect(self):
        return self

    def close(self):
        return None

    # --- assertions --------------------------------------------------
    def ran(self, fragment):
        """Was a command containing `fragment` issued?"""
        return any(fragment in command for command in self.commands)

    def count(self, fragment):
        return sum(1 for command in self.commands if fragment in command)


class RecordingLogger:
    """A RunLogger stand-in that keeps what it was told."""

    def __init__(self):
        self.infos = []
        self.warnings = []
        self.errors = []
        self.node_messages = []
        self.steps = []
        self.run_id = "test-run"
        self.root = Path("/tmp/test-run")

    def info(self, message):
        self.infos.append(str(message))

    def warn(self, message):
        self.warnings.append(str(message))

    def error(self, message):
        self.errors.append(str(message))

    def debug(self, message):
        pass

    def banner(self, title):
        self.infos.append(f"== {title}")

    def node(self, node_name, message):
        self.node_messages.append((node_name, str(message)))

    def node_log_path(self, node_name):
        return f"/tmp/test-run/{node_name}.log"

    def step_start(self, name, detail=""):
        self.steps.append({"name": name, "status": "running"})

    def step_end(self, status="passed", message=""):
        if self.steps:
            self.steps[-1].update(status=status, message=message)

    def said(self, fragment):
        """Did any info/warn/node message contain `fragment`?"""
        everything = self.infos + self.warnings + self.errors + \
            [m for _, m in self.node_messages]
        return any(fragment in message for message in everything)


@pytest.fixture(autouse=True)
def logs_in_tmp(tmp_path, monkeypatch):
    """Keep run logs out of the repository."""
    from aspects import logging_setup

    monkeypatch.setattr(logging_setup, "LOG_ROOT", tmp_path / "logs")


@pytest.fixture
def executor():
    return FakeExecutor()


@pytest.fixture
def logger():
    return RecordingLogger()


def make_hosts(*specs):
    """make_hosts(("a", "10.0.0.1"), ...) -> [Host, ...]"""
    return [Host(name=name, address=address) for name, address in specs]


@pytest.fixture
def local_host():
    """One machine, listed under its loopback name — the laptop/VM case."""
    return [Host(name="local", address="localhost", local=True)]


@pytest.fixture
def three_hosts():
    return make_hosts(("a", "10.0.1.11"), ("b", "10.0.1.12"), ("c", "10.0.1.13"))


@pytest.fixture
def plan_factory():
    """Build a planned cluster the way deployment/cli.py does."""

    def build(hosts=None, node_count=2, standby_of=None, **kwargs):
        hosts = hosts or [Host(name="local", address="localhost", local=True)]
        plan, warnings = topology.plan_cluster(
            hosts=hosts,
            cluster_name=kwargs.pop("cluster_name", "pgedge"),
            node_count=node_count,
            standby_of=standby_of,
            **kwargs,
        )
        return plan, warnings

    return build


@pytest.fixture
def deployed_plan(plan_factory):
    """A plan as it looks after step 01: probed, with routable addresses."""
    plan, _ = plan_factory(node_count=2)
    plan.pg_version = "17.11"
    plan.deploy_mode = "packages"
    for host in plan.hosts:
        host.family = "rhel"
        host.advertise_address = "10.0.1.11"
        host.bin_dir = "/usr/pgsql-17/bin"
    for node in plan.nodes:
        node.family = "rhel"
        node.bin_dir = "/usr/pgsql-17/bin"
        node.advertise_address = "10.0.1.11"
        node.pg_version = "17.11"
    return plan


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect saved cluster state into a temporary directory."""
    from aspects import state

    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "clusters")
    return tmp_path / "clusters"


@pytest.fixture
def inventory_file(tmp_path):
    """Write an inventory and return its path."""

    def write(hosts, defaults=None):
        path = tmp_path / "inventory.json"
        path.write_text(json.dumps({
            "defaults": defaults or {},
            "hosts": hosts,
        }), encoding="utf-8")
        return path

    return write
