#!/usr/bin/env python3
"""Synchronous replication: what Patroni is told, and where it is told it.

The settings live in the DCS — `bootstrap.dcs` when a scope is created, and
`patronictl edit-config` for one that already exists. Patroni owns
`synchronous_standby_names` and computes it from these; nothing here writes it.
"""

import pytest

from aspects import patroni_management as pm
from aspects.cluster_model import Host
from deployment import topology

from conftest import FakeExecutor, RecordingLogger


@pytest.fixture
def scoped_plan():
    """Two Spock nodes; only n1 has a standby."""
    hosts = [Host(name="a", address="10.0.1.11"), Host(name="b", address="10.0.1.12")]
    plan, _ = topology.plan_cluster(hosts, "pgedge", node_count=2,
                                    standby_of=["n1"])
    return plan


def dcs_of(plan, node):
    return pm.build_config(plan, node)["bootstrap"]["dcs"]


# ---------------------------------------------------------------------------
# what the words mean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given, expected", [
    ("", "off"), (None, "off"), ("off", "off"), ("async", "off"),
    ("asynchronous", "off"), (False, "off"),
    ("on", "on"), ("sync", "on"), ("synchronous", "on"), ("yes", "on"), (True, "on"),
    ("quorum", "quorum"), ("QUORUM", "quorum"),
])
def test_modes_are_normalised_to_what_patroni_accepts(given, expected):
    assert pm.normalise_sync_mode(given) == expected


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown replication mode"):
        pm.normalise_sync_mode("semi")


# ---------------------------------------------------------------------------
# the DCS block
# ---------------------------------------------------------------------------


def test_asynchronous_writes_nothing(scoped_plan):
    """off is Patroni's default; saying so explicitly buys nothing."""
    assert pm.sync_settings(scoped_plan, scoped_plan.node("n1")) == {}
    assert "synchronous_mode" not in dcs_of(scoped_plan, scoped_plan.node("n1"))


def test_synchronous_mode_and_count_reach_the_dcs(scoped_plan):
    scoped_plan.synchronous_mode = "on"
    scoped_plan.synchronous_node_count = 1

    dcs = dcs_of(scoped_plan, scoped_plan.node("n1"))

    assert dcs["synchronous_mode"] == "on"
    assert dcs["synchronous_node_count"] == 1
    assert "synchronous_standby_names" not in dcs      # Patroni computes it


def test_strict_mode_is_only_written_when_asked(scoped_plan):
    scoped_plan.synchronous_mode = "on"

    assert "synchronous_mode_strict" not in dcs_of(scoped_plan,
                                                   scoped_plan.node("n1"))

    scoped_plan.synchronous_mode_strict = True

    assert dcs_of(scoped_plan, scoped_plan.node("n1"))["synchronous_mode_strict"] \
        is True


def test_quorum_mode_is_passed_through(scoped_plan):
    scoped_plan.synchronous_mode = "quorum"
    scoped_plan.synchronous_node_count = 2

    dcs = dcs_of(scoped_plan, scoped_plan.node("n1"))

    assert (dcs["synchronous_mode"], dcs["synchronous_node_count"]) == ("quorum", 2)


def test_a_scope_with_no_standby_stays_asynchronous(scoped_plan):
    """Under strict, such a scope would refuse writes forever."""
    scoped_plan.synchronous_mode = "on"
    scoped_plan.synchronous_mode_strict = True

    assert pm.sync_settings(scoped_plan, scoped_plan.node("n2")) == {}
    assert "synchronous_mode" not in dcs_of(scoped_plan, scoped_plan.node("n2"))


def test_a_leader_may_override_the_cluster_default(scoped_plan):
    """The mode is a property of one scope, not of the whole cluster."""
    leader = scoped_plan.node("n1")
    leader.synchronous_mode = "on"
    leader.synchronous_node_count = 1

    assert scoped_plan.synchronous_mode == "off"
    assert dcs_of(scoped_plan, leader)["synchronous_mode"] == "on"


def test_the_standby_carries_no_bootstrap_block(scoped_plan):
    """A standby joins an existing scope and reads the settings from the DCS."""
    scoped_plan.synchronous_mode = "on"

    assert "bootstrap" not in pm.build_config(scoped_plan, scoped_plan.node("n1s1"))


def test_describe_sync_says_what_happens_when_the_standby_dies(scoped_plan):
    leader = scoped_plan.node("n1")

    assert "asynchronous replication" in pm.describe_sync(scoped_plan, leader)

    scoped_plan.synchronous_mode = "on"
    assert "falls back to asynchronous" in pm.describe_sync(scoped_plan, leader)

    scoped_plan.synchronous_mode_strict = True
    assert "writes block" in pm.describe_sync(scoped_plan, leader)


# ---------------------------------------------------------------------------
# applying it to a cluster that is already running
# ---------------------------------------------------------------------------


def test_a_running_scope_is_changed_through_patronictl(scoped_plan):
    """bootstrap.dcs is read once, when the scope is created."""
    scoped_plan.synchronous_mode = "on"
    scoped_plan.synchronous_node_count = 1
    executor = FakeExecutor()

    ok, _ = pm.apply_sync_settings(executor, scoped_plan, scoped_plan.node("n1"))

    assert ok is True
    command = executor.commands[-1]
    assert "edit-config pgedge-n1 --force" in command
    assert "-s synchronous_mode=on" in command
    assert "-s synchronous_node_count=1" in command


def test_strict_is_sent_as_a_lowercase_boolean(scoped_plan):
    scoped_plan.synchronous_mode = "on"
    scoped_plan.synchronous_mode_strict = True
    executor = FakeExecutor()

    pm.apply_sync_settings(executor, scoped_plan, scoped_plan.node("n1"))

    assert "-s synchronous_mode_strict=true" in executor.commands[-1]


def test_going_back_to_asynchronous_clears_the_setting(scoped_plan):
    executor = FakeExecutor()

    pm.apply_sync_settings(executor, scoped_plan, scoped_plan.node("n1"))

    assert "-s synchronous_mode=off" in executor.commands[-1]


def test_a_failed_edit_config_is_reported_not_raised(scoped_plan):
    scoped_plan.synchronous_mode = "on"
    executor = FakeExecutor({"edit-config": (False, "Error: DCS is unreachable")})

    ok, output = pm.apply_sync_settings(executor, scoped_plan, scoped_plan.node("n1"))

    assert ok is False
    assert "DCS is unreachable" in output


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_planning_accepts_the_words_and_stores_patronis(three_hosts):
    plan, _ = topology.plan_cluster(three_hosts, "pgedge", node_count=2,
                                    standby_of=["n1"], synchronous_mode="sync",
                                    synchronous_node_count=1)

    assert plan.synchronous_mode == "on"
    assert plan.synchronous_node_count == 1


def test_a_scope_without_a_standby_is_called_out(three_hosts):
    plan, warnings = topology.plan_cluster(three_hosts, "pgedge", node_count=2,
                                           standby_of=["n1"],
                                           synchronous_mode="sync")

    assert any("n2 has no standby" in w for w in warnings)


def test_asking_for_more_confirmations_than_standbys_is_called_out(three_hosts):
    plan, warnings = topology.plan_cluster(three_hosts, "pgedge", node_count=2,
                                           standby_of=["n1"],
                                           synchronous_mode="sync",
                                           synchronous_node_count=3)

    assert any("reduces the count" in w for w in warnings)


def test_strict_mode_is_called_out(three_hosts):
    _, warnings = topology.plan_cluster(three_hosts, "pgedge", node_count=1,
                                        standby_of=["n1"],
                                        synchronous_mode="sync",
                                        synchronous_mode_strict=True)

    assert any("refuses writes" in w for w in warnings)


def test_asynchronous_planning_says_nothing(three_hosts):
    _, warnings = topology.plan_cluster(three_hosts, "pgedge", node_count=2,
                                        standby_of=["n1"])

    assert not any("synchronous" in w for w in warnings)


def test_the_settings_survive_a_state_round_trip(state_dir, three_hosts):
    from aspects import state

    plan, _ = topology.plan_cluster(three_hosts, "pgedge", node_count=2,
                                    standby_of=["n1"], synchronous_mode="quorum",
                                    synchronous_node_count=2,
                                    synchronous_mode_strict=True)
    plan.node("n1").synchronous_mode = "on"
    state.save(plan)

    loaded, _ = state.load("pgedge")

    assert loaded.synchronous_mode == "quorum"
    assert loaded.synchronous_node_count == 2
    assert loaded.synchronous_mode_strict is True
    assert loaded.node("n1").synchronous_mode == "on"


# ---------------------------------------------------------------------------
# the command layer
# ---------------------------------------------------------------------------


def test_add_standby_takes_the_mode_from_the_cli(monkeypatch):
    import types
    from deployment import add_standby, ops_cli

    captured = {}
    monkeypatch.setattr(add_standby, "add",
                        lambda **kwargs: captured.update(kwargs) or
                        {"outcome": "succeeded", "log_dir": "/tmp/run"})
    monkeypatch.setattr(ops_cli, "_write_report", lambda *a, **k: None)
    monkeypatch.setattr(ops_cli.state, "latest_cluster", lambda: "pgedge")

    code = ops_cli.cmd_node_add(types.SimpleNamespace(
        cluster=None, db_password=None, inventory=None, host=None, name="n1s1",
        source=None, role="standby", leader="n1", pg_version="", spock_major="",
        spock_branch="", skip_verify=False, sync_mode="sync", sync_count=1,
        sync_strict=True))

    assert code == 0
    assert captured["synchronous_mode"] == "sync"
    assert captured["synchronous_node_count"] == 1
    assert captured["synchronous_mode_strict"] is True


def test_sync_flags_are_refused_for_a_spock_node(monkeypatch, capsys):
    import types
    from deployment import add_node, ops_cli

    monkeypatch.setattr(add_node, "add", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("should not run")))
    monkeypatch.setattr(ops_cli.state, "latest_cluster", lambda: "pgedge")

    code = ops_cli.cmd_node_add(types.SimpleNamespace(
        cluster=None, db_password=None, inventory=None, host=None, name="n3",
        source=None, role="leader", leader=None, pg_version="", spock_major="",
        spock_branch="", skip_verify=False, sync_mode="sync", sync_count=None,
        sync_strict=False))

    assert code == 2
    assert "role standby" in capsys.readouterr().err


def test_deploy_flags_are_parsed():
    from deployment.cli import build_parser

    args = build_parser().parse_args(["deploy", "--standby", "n1",
                                      "--sync-mode", "sync", "--sync-count", "2",
                                      "--sync-strict"])

    assert (args.sync_mode, args.sync_count, args.sync_strict) == ("sync", 2, True)


def test_add_standby_flags_are_parsed():
    from deployment.cli import build_parser

    args = build_parser().parse_args(["add-standby", "--leader", "n1",
                                      "--sync-mode", "quorum"])

    assert args.sync_mode == "quorum"
