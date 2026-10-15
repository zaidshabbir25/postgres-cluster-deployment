#!/usr/bin/env python3
"""PostgreSQL's own logging, on a cluster that is already running.

A cluster deployed before this existed reports every logging setting as
`source = default` with `logging_collector = off`: Patroni never set them, so
the server wrote to stderr and systemd swallowed it. Two facts shape
everything here — Patroni renders postgresql.conf itself, so the settings have
to reach it through the DCS, and `logging_collector` is read only at startup,
so storing it is not the same as having it.
"""

import pytest

from aspects import patroni_management
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK, ROLE_STANDBY
from tests.conftest import FakeExecutor


def node(name, role=ROLE_SPOCK, leader=None, scope=None, standbys=()):
    return Node(name=name, role=role, host="local", address="10.0.1.10",
                pg_port=5432, restapi_port=8008,
                data_dir=f"/var/lib/pgedge/{name}",
                scope=scope or f"pgedge-{name}",
                config_file=f"/etc/patroni/{name}.yml", pgpass_file="/p",
                bin_dir="/usr/pgsql-17/bin", leader=leader,
                standbys=list(standbys))


def plan_with(standby=False, **kwargs):
    host = Host(name="local", address="10.0.1.10")
    host.family = "rhel"
    plan = ClusterPlan(cluster_name="pgedge", pg_major="17", pg_version="17.11",
                       hosts=[host], **kwargs)
    plan.nodes = [node("n1", standbys=["n1s1"] if standby else []), node("n2")]
    if standby:
        plan.nodes.append(node("n1s1", ROLE_STANDBY, leader="n1",
                               scope="pgedge-n1"))
    return plan


# ---------------------------------------------------------------------------
# what gets set
# ---------------------------------------------------------------------------


def test_the_collector_is_on_because_otherwise_nothing_is_written():
    """stderr with no collector goes to the service manager's journal, which
    is where it was going before."""
    parameters = patroni_management.logging_parameters()

    assert parameters["logging_collector"] == "on"
    assert parameters["log_destination"] == "stderr"


def test_the_directory_is_relative_so_each_node_logs_beside_its_own_data():
    """Two nodes on one host would otherwise interleave into one place."""
    parameters = patroni_management.logging_parameters()

    assert parameters["log_directory"] == "log"
    assert not parameters["log_directory"].startswith("/")
    assert patroni_management.log_directory_for(node("n1")) == \
        "/var/lib/pgedge/n1/log"


def test_rotation_is_seven_files_that_overwrite_themselves():
    """postgresql-Mon.log through postgresql-Sun.log: no cron, no logrotate,
    and no growth."""
    parameters = patroni_management.logging_parameters()

    assert parameters["log_filename"] == "postgresql-%a.log"
    assert parameters["log_truncate_on_rotation"] == "on"
    assert parameters["log_rotation_age"] == "1d"
    assert parameters["log_rotation_size"] == "0"


def test_an_override_replaces_one_setting_and_keeps_the_rest():
    parameters = patroni_management.logging_parameters(
        {"log_min_messages": "info"})

    assert parameters["log_min_messages"] == "info"
    assert parameters["logging_collector"] == "on"


# ---------------------------------------------------------------------------
# how it reaches the server
# ---------------------------------------------------------------------------


def test_a_new_cluster_gets_logging_from_its_bootstrap_block():
    """logging_collector is read at startup, so a node that bootstraps without
    it needs a restart before it writes anything at all."""
    plan = plan_with()

    config = patroni_management.build_config(plan, plan.node("n1"))
    parameters = config["bootstrap"]["dcs"]["postgresql"]["parameters"]

    assert parameters["logging_collector"] == "on"
    assert parameters["log_filename"] == "postgresql-%a.log"


def test_a_standby_carries_no_bootstrap_block_and_needs_none():
    """It reads the scope's parameters from the DCS, which the leader wrote."""
    plan = plan_with(standby=True)

    config = patroni_management.build_config(plan, plan.node("n1s1"))

    assert "bootstrap" not in config


def test_a_running_cluster_is_configured_through_patronictl():
    """Patroni renders postgresql.conf itself, so writing to the file would be
    undone on its next reload."""
    plan = plan_with()
    executor = FakeExecutor()

    ok, _ = patroni_management.apply_logging(executor, plan, plan.node("n1"))

    assert ok
    command = next(c for c in executor.commands if "edit-config" in c)
    assert "postgresql.parameters.logging_collector=on" in command
    assert "pgedge-n1" in command


def test_the_settings_are_written_once_per_scope_not_once_per_node():
    """Every member of a scope reads the same block, so a leader and its
    standbys are configured together."""
    plan = plan_with(standby=True)

    assert len(plan.scopes()) == 2
    assert sorted(plan.scopes()) == ["pgedge-n1", "pgedge-n2"]


# ---------------------------------------------------------------------------
# stored is not the same as in force
# ---------------------------------------------------------------------------


def test_a_node_waiting_on_a_restart_is_reported_as_such():
    plan = plan_with()
    executor = FakeExecutor(replies={"pending_restart": (True, "logging_collector\n")})

    waiting = patroni_management.pending_restart(executor, plan, plan.node("n1"))

    assert waiting == ["logging_collector"]


def test_a_node_with_nothing_pending_reports_nothing():
    plan = plan_with()
    executor = FakeExecutor(replies={"pending_restart": (True, "\n")})

    assert patroni_management.pending_restart(executor, plan,
                                              plan.node("n1")) == []


def test_only_the_collector_needs_a_restart():
    """Everything else takes a reload, which is why the restart is worth
    singling out rather than restarting for any change."""
    assert patroni_management.NEEDS_RESTART == ("logging_collector",)
    for name in patroni_management.LOG_PARAMETERS:
        if name != "logging_collector":
            assert name not in patroni_management.NEEDS_RESTART
