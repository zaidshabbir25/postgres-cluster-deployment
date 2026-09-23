#!/usr/bin/env python3
"""The server's own log: where it lands, and how to reach it.

Patroni's unit journal says what Patroni did; this is PostgreSQL's account of
itself — checkpoints, lock waits, recovery, the statements that failed. Without
logging_collector there is no file at all, which is what this covers.
"""

import pytest

from aspects import db_operations, patroni_management as pm, pg_server_management as pgsm

from conftest import FakeExecutor


def parameters_of(plan, node):
    return pm.build_config(plan, node)["bootstrap"]["dcs"]["postgresql"]["parameters"]


# ---------------------------------------------------------------------------
# where it lands
# ---------------------------------------------------------------------------


def test_the_log_directory_is_inside_the_data_directory(deployed_plan):
    """Relative log_directory is resolved against the data directory, so two
    nodes on one machine cannot write over each other."""
    assert pgsm.log_directory(deployed_plan.node("n1")) == "/var/lib/pgedge/n1/log"
    assert pgsm.log_directory(deployed_plan.node("n2")) == "/var/lib/pgedge/n2/log"


def test_the_collector_is_on_and_writes_a_relative_directory(deployed_plan):
    parameters = parameters_of(deployed_plan, deployed_plan.node("n1"))

    assert parameters["logging_collector"] == "on"
    assert parameters["log_directory"] == "log"          # not an absolute path
    assert parameters["log_destination"] == "stderr"


def test_rotation_keeps_the_directory_bounded(deployed_plan):
    parameters = parameters_of(deployed_plan, deployed_plan.node("n1"))

    assert parameters["log_filename"] == "postgresql-%a.log"   # one per weekday
    assert parameters["log_rotation_age"] == "1d"
    assert parameters["log_truncate_on_rotation"] == "on"


def test_a_longer_retention_switches_to_dated_files():
    """A week of weekday names cannot hold 30 days."""
    weekly = pgsm.logging_parameters(7)
    monthly = pgsm.logging_parameters(30)

    assert weekly["log_filename"] == "postgresql-%a.log"
    assert monthly["log_filename"] == "postgresql-%Y-%m-%d.log"


def test_retention_is_clamped_to_something_sane():
    assert pgsm.logging_parameters(0)["log_filename"] == "postgresql-%a.log"
    assert pgsm.logging_parameters(999)["log_filename"] == "postgresql-%Y-%m-%d.log"


def test_the_log_file_is_not_world_readable(deployed_plan):
    assert parameters_of(deployed_plan, deployed_plan.node("n1"))["log_file_mode"] \
        == "0600"


def test_the_prefix_identifies_the_session(deployed_plan):
    prefix = parameters_of(deployed_plan, deployed_plan.node("n1"))["log_line_prefix"]

    for placeholder in ("%m", "%p", "%u", "%d"):
        assert placeholder in prefix


def test_logging_does_not_disturb_the_spock_gucs(deployed_plan):
    parameters = parameters_of(deployed_plan, deployed_plan.node("n1"))

    assert parameters["wal_level"] == "logical"
    assert "spock" in parameters["shared_preload_libraries"]
    assert parameters["track_commit_timestamp"] == "on"


def test_a_standby_inherits_the_settings_from_its_scope(plan_factory):
    """Standbys carry no bootstrap block; the DCS is what governs them."""
    plan, _ = plan_factory(node_count=1, standby_of=["n1"])

    assert "bootstrap" not in pm.build_config(plan, plan.node("n1s1"))
    assert pgsm.log_directory(plan.node("n1s1")) == "/var/lib/pgedge/n1s1/log"


# ---------------------------------------------------------------------------
# switching it on for a cluster that already exists
# ---------------------------------------------------------------------------


def test_a_running_scope_is_changed_through_patronictl(deployed_plan):
    executor = FakeExecutor()

    ok, detail = pm.apply_logging_settings(executor, deployed_plan,
                                           deployed_plan.node("n1"))

    assert ok is True
    command = executor.commands[-1]
    assert "edit-config pgedge-n1 --force" in command
    assert "postgresql.parameters.logging_collector=on" in command
    assert "postgresql.parameters.log_directory=log" in command


def test_it_says_a_restart_is_needed(deployed_plan):
    """logging_collector cannot be changed by a reload."""
    ok, detail = pm.apply_logging_settings(FakeExecutor(), deployed_plan,
                                           deployed_plan.node("n1"))

    assert "restart" in detail
    assert "/var/lib/pgedge/n1/log" in detail


def test_a_failure_is_reported_not_raised(deployed_plan):
    executor = FakeExecutor({"edit-config": (False, "Error: DCS is unreachable")})

    ok, detail = pm.apply_logging_settings(executor, deployed_plan,
                                           deployed_plan.node("n1"))

    assert ok is False
    assert "DCS is unreachable" in detail


# ---------------------------------------------------------------------------
# reading it
# ---------------------------------------------------------------------------


def test_the_newest_file_is_read_by_default(deployed_plan):
    node = deployed_plan.node("n1")
    executor = FakeExecutor({
        "ls -1t": (True, "/var/lib/pgedge/n1/log/postgresql-Tue.log\n"
                         "/var/lib/pgedge/n1/log/postgresql-Mon.log\n"),
        "tail -n": (True, "2026-09-23 10:00:00 UTC [123] LOG:  checkpoint starting"),
    })

    path, text = db_operations.server_log(executor, deployed_plan, node)

    assert path == "/var/lib/pgedge/n1/log/postgresql-Tue.log"
    assert "checkpoint starting" in text


def test_a_specific_file_can_be_asked_for(deployed_plan):
    node = deployed_plan.node("n1")
    wanted = "/var/lib/pgedge/n1/log/postgresql-Mon.log"
    executor = FakeExecutor({"ls -1t": (True, f"{wanted}\n"), "tail -n": (True, "x")})

    path, _ = db_operations.server_log(executor, deployed_plan, node,
                                       follow_file=wanted)

    assert path == wanted
    assert executor.ran(f"tail -n 100 {wanted}")


def test_no_log_file_says_how_to_get_one(deployed_plan):
    node = deployed_plan.node("n1")
    executor = FakeExecutor({"ls -1t": (True, "")})

    path, text = db_operations.server_log(executor, deployed_plan, node)

    assert path == ""
    assert "/var/lib/pgedge/n1/log" in text
    assert "logging-on" in text


def test_the_line_count_is_passed_through(deployed_plan):
    node = deployed_plan.node("n1")
    executor = FakeExecutor({"ls -1t": (True, "/var/lib/pgedge/n1/log/a.log\n"),
                             "tail -n": (True, "")})

    db_operations.server_log(executor, deployed_plan, node, lines=500)

    assert executor.ran("tail -n 500 /var/lib/pgedge/n1/log/a.log")


# ---------------------------------------------------------------------------
# the command layer
# ---------------------------------------------------------------------------


def test_the_db_group_exposes_logs_and_logging_on():
    from deployment.cli import build_parser

    parser = build_parser()

    logs = parser.parse_args(["db", "logs", "--node", "n2", "--lines", "50"])
    enable = parser.parse_args(["db", "logging-on", "--all-nodes"])

    assert (logs.db_action, logs.node, logs.lines) == ("logs", "n2", 50)
    assert (enable.db_action, enable.all_nodes) == ("logging-on", True)
