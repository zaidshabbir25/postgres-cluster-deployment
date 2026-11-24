#!/usr/bin/env python3
"""The settings every node is bootstrapped with, and how they are confirmed.

They travel through the DCS because that is the only route that reaches every
member of a scope — the standby, and a node added later — and because an
ALTER SYSTEM would shadow the DCS value from then on. So the tests pin two
things: that the DCS carries them, and that the post-cross-wire step confirms
rather than overwrites.
"""

import pytest

from aspects import patroni_management, pg_server_management
from aspects import spock_management as sm
from aspects.cluster_model import Node

from conftest import FakeExecutor, RecordingLogger

REQUESTED = {
    "track_commit_timestamp": "on",
    "spock.enable_ddl_replication": "on",
    "spock.allow_ddl_from_functions": "on",
    "spock.include_ddl_repset": "on",
    "spock.conflict_log_level": "debug2",
    "log_min_messages": "debug1",
}


def a_node(name="n1"):
    return Node(name=name, role="spock", host="local", address="localhost",
                pg_port=5432, restapi_port=8008, data_dir="/d",
                scope=f"pgedge-{name}", config_file="/c", pgpass_file="/p",
                bin_dir="/usr/pgsql-17/bin")


# ---------------------------------------------------------------------------
# bootstrapped through the DCS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("guc,value", REQUESTED.items())
def test_every_requested_setting_is_in_the_bootstrap_parameters(guc, value,
                                                               deployed_plan):
    """Leaders carry the bootstrap block; standbys read it from the DCS, so
    one block is every node."""
    config = patroni_management.build_config(deployed_plan,
                                             deployed_plan.spock_nodes[0])
    parameters = config["bootstrap"]["dcs"]["postgresql"]["parameters"]

    assert parameters[guc] == value


def test_the_spock_settings_come_from_one_place():
    """SPOCK_PARAMETERS is what the DCS gets and what verify_gucs checks;
    the two must not drift apart."""
    for guc, value in pg_server_management.SPOCK_PARAMETERS.items():
        assert sm.REQUIRED_GUCS[guc] == value


def test_the_log_level_the_dcs_gets_is_the_one_verified():
    assert patroni_management.LOG_PARAMETERS["log_min_messages"] == \
        sm.REQUIRED_GUCS["log_min_messages"] == "debug1"


def test_a_lower_log_level_can_still_be_chosen_per_cluster(deployed_plan):
    """debug1 is the default, not a requirement: --logging --set overrides."""
    deployed_plan.log_parameters = {"log_min_messages": "warning"}
    config = patroni_management.build_config(deployed_plan,
                                             deployed_plan.spock_nodes[0])

    assert config["bootstrap"]["dcs"]["postgresql"]["parameters"][
        "log_min_messages"] == "warning"


# ---------------------------------------------------------------------------
# confirmed, not overwritten
# ---------------------------------------------------------------------------


def showing(values):
    """An executor whose SHOW answers are scripted per GUC."""
    replies = {f"SHOW {guc};": (True, f"{value}\n") for guc, value in values.items()}
    return FakeExecutor(replies=replies)


def test_a_node_running_the_settings_is_left_alone(deployed_plan):
    """ALTER SYSTEM would write postgresql.auto.conf, which shadows the DCS
    for good — so when the DCS did its job, nothing is written."""
    executor = showing({guc: "on" for guc in sm.DDL_REPLICATION_GUCS})
    log = RecordingLogger()

    changed = sm.enable_ddl_replication(executor, deployed_plan, a_node(),
                                        run_logger=log)

    assert changed == []
    assert not executor.ran("ALTER SYSTEM")
    assert not executor.ran("pg_reload_conf")


def test_a_node_missing_one_setting_gets_only_that_one(deployed_plan):
    """The fallback for a node Patroni did not bootstrap: set what is off,
    and nothing that is already on."""
    executor = showing({
        "spock.enable_ddl_replication": "on",
        "spock.include_ddl_repset": "off",
        "spock.allow_ddl_from_functions": "on",
    })

    changed = sm.enable_ddl_replication(executor, deployed_plan, a_node())

    assert changed == ["spock.include_ddl_repset"]
    alters = [c for c in executor.commands if "ALTER SYSTEM" in c]
    assert len(alters) == 1 and "include_ddl_repset" in alters[0]
    assert executor.ran("pg_reload_conf")


def test_verify_reports_each_setting_that_differs_with_both_values(deployed_plan):
    executor = showing(dict(REQUESTED, **{"spock.conflict_log_level": "log",
                                          "log_min_messages": "warning"}))

    wrong = sm.verify_gucs(executor, deployed_plan, a_node())

    assert wrong == [("spock.conflict_log_level", "log", "debug2"),
                     ("log_min_messages", "warning", "debug1")]


def test_verify_treats_an_unanswered_show_as_unset(deployed_plan):
    executor = FakeExecutor(replies={"SHOW": (False, "")})

    wrong = sm.verify_gucs(executor, deployed_plan, a_node(),
                           wanted={"spock.enable_ddl_replication": "on"})

    assert wrong == [("spock.enable_ddl_replication", "(unset)", "on")]


def test_verify_compares_case_insensitively(deployed_plan):
    """SHOW spells booleans 'on'; a GUC that echoes 'ON' or 'DEBUG2' is still
    right."""
    executor = showing({"spock.conflict_log_level": "DEBUG2",
                        "track_commit_timestamp": "ON"})

    assert sm.verify_gucs(executor, deployed_plan, a_node(), wanted={
        "spock.conflict_log_level": "debug2",
        "track_commit_timestamp": "on"}) == []


# ---------------------------------------------------------------------------
# standbys' slot copies: who makes them depends on the Spock major
# ---------------------------------------------------------------------------


def _parameters(plan):
    config = patroni_management.build_config(plan, plan.spock_nodes[0])
    return config["bootstrap"]["dcs"]["postgresql"]["parameters"]


@pytest.mark.parametrize("pg_version", ["17.11", "18.7", "19beta3"])
def test_a_spock6_scope_on_pg17_or_later_turns_native_slot_sync_on(
        deployed_plan, pg_version):
    """Spock 6 registers no failover-slots worker on 18 and yields to
    PostgreSQL's own on 17, so without this a standby carries no copy of
    the spk_* slots and a promotion leaves the peers with nothing to
    connect to. The DCS carries it so every member, present and future,
    has it the moment it becomes a standby."""
    deployed_plan.spock_major = "60"
    deployed_plan.pg_version = pg_version

    assert _parameters(deployed_plan)["sync_replication_slots"] == "on"
    assert _parameters(deployed_plan)["hot_standby_feedback"] == "on"


def test_a_spock5_scope_keeps_spocks_own_worker(deployed_plan):
    """Spock 5 copies the slots itself on every version; turning native sync
    on would make it stand aside, which is a change of mechanism no one
    asked for."""
    deployed_plan.spock_major = "50"
    deployed_plan.pg_version = "18.7"

    assert "sync_replication_slots" not in _parameters(deployed_plan)


def test_a_spock6_scope_on_pg16_has_no_native_sync_to_turn_on(deployed_plan):
    deployed_plan.spock_major = "60"
    deployed_plan.pg_version = "16.9"

    assert "sync_replication_slots" not in _parameters(deployed_plan)


def test_the_slot_sync_decision_is_made_in_one_place():
    gucs = pg_server_management.native_slot_sync_gucs
    assert gucs("18.7", "60") == {"sync_replication_slots": "on"}
    assert gucs("17", "60") == {"sync_replication_slots": "on"}
    assert gucs("16.9", "60") == {}
    assert gucs("18.7", "50") == {}
    assert gucs("", "60") == {}
