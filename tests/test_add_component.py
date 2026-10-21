#!/usr/bin/env python3
"""Adding lolor, snowflake or ACE to a cluster that is already running.

Different from choosing them at deploy time: the nodes exist, they are
cross-wired and they are serving. lolor sets the shape, because it has to be in
shared_preload_libraries and that is read only at startup — so there is a
restart between installing it and being able to create it, and the step that
creates it has to refuse to run before that restart has happened.
"""

import pytest

from aspects import patroni_management, pg_extensions
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK
from deployment import add_component
from tests.conftest import FakeExecutor


def node(name, port=5432):
    return Node(name=name, role=ROLE_SPOCK, host="local", address="10.0.1.10",
                pg_port=port, restapi_port=8008,
                data_dir=f"/var/lib/pgedge/{name}", scope=f"pgedge-{name}",
                config_file=f"/etc/patroni/{name}.yml", pgpass_file="/p",
                bin_dir="/usr/pgsql-17/bin")


def plan_with(**kwargs):
    host = Host(name="local", address="10.0.1.10")
    host.family = "rhel"
    plan = ClusterPlan(cluster_name="pgedge", pg_major="17", pg_version="17.11",
                       hosts=[host], **kwargs)
    plan.nodes = [node("n1"), node("n2", 5433)]
    return plan


# ---------------------------------------------------------------------------
# what the cluster ends up recording
# ---------------------------------------------------------------------------


def test_a_new_component_is_added_to_what_the_cluster_records():
    """So a later --add-node gives the joining node the same thing."""
    existing = pg_extensions.parse("pgedge-snowflake")
    additions = pg_extensions.parse("pgedge-lolor")

    merged = add_component.merge(existing, additions)

    assert [item["name"] for item in merged] == ["snowflake", "lolor"]


def test_restating_a_component_does_not_change_what_it_was_built_from():
    """Re-stating it should not quietly rebuild it from a different ref."""
    existing = pg_extensions.parse("pgedge-lolor:source@v1.2.3")
    additions = pg_extensions.parse("pgedge-lolor:source@main")

    merged = add_component.merge(existing, additions)

    assert len(merged) == 1
    assert merged[0]["ref"] == "v1.2.3"


def test_merging_nothing_leaves_the_cluster_as_it_was():
    existing = pg_extensions.parse("pgedge-lolor")

    assert add_component.merge(existing, []) == existing


# ---------------------------------------------------------------------------
# shared_preload_libraries on a running cluster
# ---------------------------------------------------------------------------


def test_the_preload_list_is_extended_never_replaced():
    """Setting it to just lolor would unload spock and take the cluster's
    replication with it."""
    plan = plan_with()
    executor = FakeExecutor(replies={
        "SHOW shared_preload_libraries": (True, "spock\n")})

    ok, value = patroni_management.apply_preload(
        executor, plan, plan.node("n1"), ["lolor"])

    assert ok and value == "spock,lolor"
    command = next(c for c in executor.commands if "edit-config" in c)
    assert "shared_preload_libraries=spock,lolor" in command


def test_a_library_already_loaded_is_not_written_again():
    """An edit-config that changes nothing is still a write to the DCS."""
    plan = plan_with()
    executor = FakeExecutor(replies={
        "SHOW shared_preload_libraries": (True, "spock,lolor\n")})

    ok, value = patroni_management.apply_preload(
        executor, plan, plan.node("n1"), ["lolor"])

    assert ok and value == "spock,lolor"
    assert not any("edit-config" in c for c in executor.commands)


def test_a_restart_goes_through_patroni_so_the_dcs_stays_in_step():
    plan = plan_with()
    executor = FakeExecutor()

    patroni_management.restart_member(executor, plan.node("n1"))

    command = next(c for c in executor.commands if "restart" in c)
    assert "patronictl" in command
    assert "pgedge-n1 n1 --force" in command


# ---------------------------------------------------------------------------
# the ordering the restart forces
# ---------------------------------------------------------------------------


def test_the_extension_is_not_created_before_the_restart_that_enables_it():
    """CREATE EXTENSION lolor fails with 'must be loaded via
    shared_preload_libraries' until the node has restarted, so attempting it
    would turn a known-incomplete state into a failure."""
    from pathlib import Path

    source = Path("deployment/add_component.py").read_text()
    guard = source.index("if in_database and pending:")
    creating = source.index("pg_extensions.create_on_node(")

    assert guard < creating


def test_an_unrestarted_node_leaves_the_run_incomplete_not_failed():
    """Everything was installed and configured; a restart is all that is
    left, and that reads differently from a failure."""
    from pathlib import Path

    source = Path("deployment/add_component.py").read_text()

    assert '"incomplete" if pending else "succeeded"' in source


def test_ace_needs_no_restart_because_it_needs_no_preload():
    assert pg_extensions.preload_libraries(
        pg_extensions.parse("pgedge-ace,pgedge-snowflake")) == []
    assert pg_extensions.preload_libraries(
        pg_extensions.parse("pgedge-lolor")) == ["lolor"]


def test_lolors_tables_are_replicated_when_it_is_added_later():
    """Added to a running cluster it is just as inert without this: large
    objects would stay on the node that made them."""
    from pathlib import Path

    source = Path("deployment/add_component.py").read_text()

    assert "REPLICATED_TABLES" in source
    assert "replicate_tables(" in source


# ---------------------------------------------------------------------------
# what it refuses
# ---------------------------------------------------------------------------


def test_naming_nothing_is_refused():
    result = add_component.add("pgedge", "", db_password="x")

    assert result["outcome"] == "failed"
    assert "name at least one component" in result["failure"]


def test_an_unknown_component_is_refused_by_name():
    result = add_component.add("pgedge", "pgedge-timescale", db_password="x")

    assert result["outcome"] == "failed"
    assert "unknown extension 'pgedge-timescale'" in result["failure"]
    assert "pgedge-lolor" in result["failure"], "it should say what does exist"


# ---------------------------------------------------------------------------
# is it actually on every node?
# ---------------------------------------------------------------------------

SHOW_CONFIG = """\
postgresql:
  parameters:
    shared_preload_libraries: spock,lolor
    wal_level: logical
"""


def test_the_dcs_is_what_is_compared_against_not_the_running_server():
    """SHOW answers what this backend loaded, which for anything needing a
    restart is the old value — so a re-run would write the same thing again
    and could never tell "configured, waiting" from "never configured"."""
    plan = plan_with()
    executor = FakeExecutor(replies={
        "show-config": (True, SHOW_CONFIG),
        "SHOW shared_preload_libraries": (True, "spock\n"),
    })

    ok, value = patroni_management.apply_preload(
        executor, plan, plan.node("n1"), ["lolor"])

    assert ok and value == "spock,lolor"
    assert not any("edit-config" in c for c in executor.commands), \
        "the DCS already has it"


def test_a_scope_with_no_dcs_answer_still_extends_what_is_running():
    """An older Patroni, or a scope that has not published its config. Losing
    spock would be far worse than a redundant write."""
    plan = plan_with()
    executor = FakeExecutor(replies={
        "show-config": (False, ""),
        "SHOW shared_preload_libraries": (True, "spock\n"),
    })

    ok, value = patroni_management.apply_preload(
        executor, plan, plan.node("n1"), ["lolor"])

    assert ok and value == "spock,lolor"
    assert any("shared_preload_libraries=spock,lolor" in c
               for c in executor.commands)


def test_an_edit_that_changed_nothing_is_not_reported_as_success():
    """It would otherwise surface much later, as CREATE EXTENSION refusing to
    run on a node everything claimed was configured."""
    plan = plan_with()
    executor = FakeExecutor(replies={
        "show-config": (True, "postgresql:\n  parameters:\n"
                              "    shared_preload_libraries: spock\n"),
        "SHOW shared_preload_libraries": (True, "spock\n"),
    })

    ok, detail = patroni_management.apply_preload(
        executor, plan, plan.node("n1"), ["lolor"])

    assert not ok
    assert "still says spock" in detail


def test_the_report_separates_configured_from_loaded():
    """They differ exactly while a restart is outstanding, which is the state
    this operation has to be able to describe."""
    plan = plan_with()
    executor = FakeExecutor(replies={
        "show-config": (True, SHOW_CONFIG),
        "SHOW shared_preload_libraries": (True, "spock\n"),
    })

    rows = patroni_management.preload_report(
        lambda node: executor, plan, ["lolor"])

    assert [row["node"] for row in rows] == ["n1", "n2"]
    assert all(row["missing_from_dcs"] == [] for row in rows)
    assert all(row["not_loaded"] == ["lolor"] for row in rows)


def test_a_node_that_never_got_the_setting_is_named():
    plan = plan_with()
    executor = FakeExecutor(replies={
        "show-config": (True, "postgresql:\n  parameters:\n"
                              "    shared_preload_libraries: spock\n"),
        "SHOW shared_preload_libraries": (True, "spock\n"),
    })

    rows = patroni_management.preload_report(
        lambda node: executor, plan, ["lolor"])

    assert all(row["missing_from_dcs"] == ["lolor"] for row in rows)


def test_every_node_is_checked_not_every_scope():
    """A standby has to load the library too, or it cannot replay what its
    leader writes."""
    from pathlib import Path

    source = Path("deployment/add_component.py").read_text()

    assert "preload_report(" in source
    assert "for node in plan.nodes" in \
        source[source.index("def "):] or "plan.nodes" in source


def test_the_restart_is_verified_rather_than_assumed():
    """A node that comes back without the library is the failure this step
    exists to prevent, and it is silent otherwise."""
    from pathlib import Path

    source = Path("deployment/add_component.py").read_text()
    restart = source.index("restart_member(")
    recheck = source.index("preload_report(", restart)

    assert restart < recheck
    assert "restarted but still does not load" in source
