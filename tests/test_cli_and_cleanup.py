#!/usr/bin/env python3
"""The command layer: what each flag dispatches to, and what it refuses."""

import types

import pytest

from deployment import add_node, add_standby, cleanup, ops_cli
from deployment.cli import build_parser


def node_add_args(**overrides):
    args = dict(cluster=None, db_password=None, inventory=None, host=None,
                name="n3", source=None, role="leader", leader=None,
                pg_version="", spock_major="", spock_branch="",
                sync_mode=None, sync_count=None, sync_strict=False,
                skip_verify=False)
    args.update(overrides)
    return types.SimpleNamespace(**args)


@pytest.fixture
def dispatched(monkeypatch):
    """Capture which add path ran, without running it."""
    calls = []

    def record(name):
        def fake(**kwargs):
            calls.append((name, kwargs))
            return {"outcome": "succeeded", "log_dir": "/tmp/run"}
        return fake

    monkeypatch.setattr(add_node, "add", record("add_node"))
    monkeypatch.setattr(add_standby, "add", record("add_standby"))
    monkeypatch.setattr(ops_cli, "_write_report", lambda *a, **k: None)
    monkeypatch.setattr(ops_cli.state, "latest_cluster", lambda: "pgedge")
    return calls


# ---------------------------------------------------------------------------
# node add
# ---------------------------------------------------------------------------


def test_a_leader_goes_to_add_node_with_its_versions(dispatched):
    code = ops_cli.cmd_node_add(node_add_args(pg_version="18.2",
                                              spock_major="60",
                                              spock_branch="main"))

    assert code == 0
    name, kwargs = dispatched[0]
    assert name == "add_node"
    assert kwargs["pg_version"] == "18.2"
    assert kwargs["spock_major"] == "60"
    assert kwargs["spock_branch"] == "main"


def test_a_standby_goes_to_add_standby(dispatched):
    code = ops_cli.cmd_node_add(node_add_args(role="standby", leader="n1",
                                              host="node-d"))

    assert code == 0
    name, kwargs = dispatched[0]
    assert name == "add_standby"
    assert kwargs["leader_name"] == "n1"
    assert kwargs["host_name"] == "node-d"


def test_a_standby_needs_a_leader(dispatched):
    assert ops_cli.cmd_node_add(node_add_args(role="standby")) == 2
    assert dispatched == []


def test_a_leader_may_not_take_a_leader(dispatched):
    assert ops_cli.cmd_node_add(node_add_args(leader="n1")) == 2
    assert dispatched == []


@pytest.mark.parametrize("field", ["pg_version", "spock_major", "spock_branch"])
def test_a_standby_takes_no_version_flags(dispatched, field):
    """A physical replica runs exactly what its leader runs."""
    args = node_add_args(role="standby", leader="n1", **{field: "60"})

    assert ops_cli.cmd_node_add(args) == 2
    assert dispatched == []


def test_an_unknown_role_is_refused(dispatched):
    assert ops_cli.cmd_node_add(node_add_args(role="witness")) == 2


def test_a_failed_add_becomes_a_non_zero_exit(monkeypatch):
    monkeypatch.setattr(ops_cli.state, "latest_cluster", lambda: "pgedge")
    monkeypatch.setattr(add_node, "add", lambda **kwargs: {
        "outcome": "failed", "failure": "boom", "log_dir": "/tmp/run"})

    assert ops_cli.cmd_node_add(node_add_args()) == 1


def test_no_cluster_at_all_is_reported_once(monkeypatch, capsys):
    monkeypatch.setattr(ops_cli.state, "latest_cluster", lambda: None)

    assert ops_cli.cmd_node_add(node_add_args()) == 1
    assert capsys.readouterr().err.count("No deployed clusters") == 1


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def test_node_add_accepts_the_documented_flags():
    parser = build_parser()

    args = parser.parse_args(["node", "add", "--name", "n3", "--role", "standby",
                              "--leader", "n1", "--host", "node-d",
                              "--pg-version", "18.2", "--spock-major", "60",
                              "--spock-branch", "main"])

    assert (args.name, args.role, args.leader) == ("n3", "standby", "n1")
    assert (args.pg_version, args.spock_major, args.spock_branch) == \
        ("18.2", "60", "main")


def test_spock_major_is_constrained():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["node", "add", "--spock-major", "70"])


def test_cleanup_is_its_own_command():
    args = build_parser().parse_args(["cleanup", "--purge", "--yes",
                                      "--inventory", "inv.json"])

    assert (args.purge, args.yes, args.inventory) == (True, True, "inv.json")


def test_plan_and_deploy_take_the_placement_flags():
    parser = build_parser()

    plan = parser.parse_args(["plan", "--nodes", "3", "--base-port", "6432",
                              "--base-restapi-port", "8108"])
    deploy = parser.parse_args(["deploy", "--mode", "source",
                                "--pg-version", "17.11", "--clean"])

    assert (plan.nodes, plan.base_port, plan.base_restapi_port) == (3, 6432, 8108)
    assert (deploy.mode, deploy.pg_version, deploy.clean) == ("source", "17.11", True)


# ---------------------------------------------------------------------------
# host cleanup
# ---------------------------------------------------------------------------


def test_wipe_hosts_removes_every_patroni_unit_it_finds(monkeypatch, tmp_path):
    """It works from the inventory: a failed deploy leaves state nothing knows."""
    from conftest import FakeExecutor
    from aspects.cluster_model import Host

    executor = FakeExecutor({
        "list-unit-files": (True, "patroni-n1.service\npatroni-n2s1.service\n"),
    })
    monkeypatch.setattr(cleanup, "build_executor", lambda *a, **k: executor)
    monkeypatch.setattr(cleanup.state, "list_clusters", lambda: [])

    result = cleanup.wipe_hosts([Host(name="local", address="localhost", local=True)],
                                data_root="/var/lib/pgedge")

    assert result["outcome"] == "succeeded"
    assert executor.ran("systemctl disable --now patroni-n1.service")
    assert executor.ran("systemctl disable --now patroni-n2s1.service")
    assert executor.ran("rm -rf /var/lib/pgedge")
    assert executor.ran("rm -rf /etc/patroni")
    assert executor.ran("pkill -f '/etc/patroni/'")


def test_wipe_hosts_survives_an_unreachable_host(monkeypatch):
    from aspects.cluster_model import Host

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(cleanup, "build_executor", refuse)
    monkeypatch.setattr(cleanup.state, "list_clusters", lambda: [])

    result = cleanup.wipe_hosts([Host(name="gone", address="10.0.0.9")])

    assert result["outcome"] == "failed"
    assert "gone" in result["unreachable"]


def test_wipe_hosts_forgets_state_describing_only_those_hosts(monkeypatch):
    from conftest import FakeExecutor
    from aspects.cluster_model import ClusterPlan, Host

    host = Host(name="local", address="localhost", local=True)
    plan = ClusterPlan(cluster_name="pgedge", hosts=[host])
    deleted = []
    monkeypatch.setattr(cleanup, "build_executor",
                        lambda *a, **k: FakeExecutor())
    monkeypatch.setattr(cleanup.state, "list_clusters", lambda: ["pgedge"])
    monkeypatch.setattr(cleanup.state, "load", lambda name: (plan, {}))
    monkeypatch.setattr(cleanup.state, "delete", lambda name: deleted.append(name))

    result = cleanup.wipe_hosts([host])

    assert deleted == ["pgedge"]
    assert result["clusters_forgotten"] == ["pgedge"]


def test_wipe_hosts_keeps_state_that_spans_other_hosts(monkeypatch):
    from conftest import FakeExecutor
    from aspects.cluster_model import ClusterPlan, Host

    here = Host(name="a", address="10.0.1.11")
    elsewhere = Host(name="b", address="10.0.1.12")
    plan = ClusterPlan(cluster_name="pgedge", hosts=[here, elsewhere])
    deleted = []
    monkeypatch.setattr(cleanup, "build_executor", lambda *a, **k: FakeExecutor())
    monkeypatch.setattr(cleanup.state, "list_clusters", lambda: ["pgedge"])
    monkeypatch.setattr(cleanup.state, "load", lambda name: (plan, {}))
    monkeypatch.setattr(cleanup.state, "delete", lambda name: deleted.append(name))

    cleanup.wipe_hosts([here])

    assert deleted == []


def test_cleanup_asks_before_wiping(monkeypatch, capsys, inventory_file):
    from deployment import cli

    path = inventory_file([{"name": "local", "host": "localhost", "local": True}])
    called = []
    monkeypatch.setattr(cleanup, "wipe_hosts",
                        lambda *a, **k: called.append(True) or {"outcome": "succeeded",
                                                                "unreachable": {}})
    monkeypatch.setattr("builtins.input", lambda *a: "no")

    code = cli.cmd_cleanup(types.SimpleNamespace(
        inventory=str(path), data_root=None, db_user=None, purge=False, yes=False))

    assert code == 0
    assert called == []
    assert "Aborted" in capsys.readouterr().out


def test_cleanup_proceeds_on_confirmation(monkeypatch, inventory_file):
    from deployment import cli

    path = inventory_file([{"name": "local", "host": "localhost", "local": True}])
    called = []
    monkeypatch.setattr(cleanup, "wipe_hosts",
                        lambda *a, **k: called.append(k) or {"outcome": "succeeded",
                                                             "unreachable": {}})
    monkeypatch.setattr("builtins.input", lambda *a: "wipe")

    code = cli.cmd_cleanup(types.SimpleNamespace(
        inventory=str(path), data_root=None, db_user=None, purge=True, yes=False))

    assert code == 0
    assert called[0]["purge_packages"] is True


def test_cleanup_skips_the_prompt_with_yes(monkeypatch, inventory_file):
    from deployment import cli

    path = inventory_file([{"name": "local", "host": "localhost", "local": True}])
    monkeypatch.setattr(cleanup, "wipe_hosts",
                        lambda *a, **k: {"outcome": "succeeded", "unreachable": {}})
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(
        AssertionError("should not ask")))

    assert cli.cmd_cleanup(types.SimpleNamespace(
        inventory=str(path), data_root=None, db_user=None,
        purge=False, yes=True)) == 0
