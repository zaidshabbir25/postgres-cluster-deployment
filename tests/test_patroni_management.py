#!/usr/bin/env python3
"""Patroni: the config it is given, and how a failed start is diagnosed.

The waits are the part worth testing hardest. Every one of these cases cost a
seven-minute silence before the loop learned to tell them apart.
"""

import json

from aspects import patroni_management as pm
from aspects.cluster_model import Node

from conftest import FakeExecutor, RecordingLogger


def a_node(name="n1", spock_major="", **kwargs):
    defaults = dict(
        role="spock", host="local", address="localhost", pg_port=5432,
        restapi_port=8008, data_dir="/var/lib/pgedge/n1", scope="pgedge-n1",
        config_file="/etc/patroni/n1.yml", pgpass_file="/p",
        bin_dir="/usr/pgsql-17/bin",
    )
    defaults.update(kwargs)
    node = Node(name=name, **defaults)
    node.spock_major = spock_major
    return node


def members_json(*members):
    return json.dumps([
        {"Cluster": "pgedge-n1", "Member": name, "Role": role, "State": state}
        for name, role, state in members
    ])


# ---------------------------------------------------------------------------
# etcd3 block
# ---------------------------------------------------------------------------


def test_etcd3_hosts_are_bare_host_port():
    """Patroni parses host:port; a URL becomes http://http://host:port."""
    assert pm.etcd3_settings(["http://localhost:2379"]) == {
        "hosts": ["localhost:2379"]
    }


def test_etcd3_handles_several_members_and_tls():
    assert pm.etcd3_settings(
        ["http://10.0.1.11:2379", "http://10.0.1.12:2379"]
    ) == {"hosts": ["10.0.1.11:2379", "10.0.1.12:2379"]}
    assert pm.etcd3_settings(["https://secure:2379"]) == {
        "hosts": ["secure:2379"], "protocol": "https",
    }


def test_etcd3_supplies_the_default_port():
    assert pm.etcd3_settings(["localhost"]) == {"hosts": ["localhost:2379"]}


# ---------------------------------------------------------------------------
# rendered configuration
# ---------------------------------------------------------------------------


def test_connect_address_never_advertises_loopback(deployed_plan):
    """Patroni rejects a loopback connect_address outright."""
    node = deployed_plan.node("n1")

    config = pm.build_config(deployed_plan, node)

    assert config["restapi"]["listen"] == f"0.0.0.0:{node.restapi_port}"
    assert config["restapi"]["connect_address"] == f"10.0.1.11:{node.restapi_port}"
    assert config["postgresql"]["connect_address"] == f"10.0.1.11:{node.pg_port}"


def test_connect_address_falls_back_to_the_listed_address(plan_factory):
    plan, _ = plan_factory()
    node = plan.node("n1")           # no advertise_address probed yet

    config = pm.build_config(plan, node)

    assert config["restapi"]["connect_address"] == "localhost:8008"


def test_only_leaders_carry_a_bootstrap_block(plan_factory):
    plan, _ = plan_factory(node_count=1, standby_of=["n1"])

    leader = pm.build_config(plan, plan.node("n1"))
    standby = pm.build_config(plan, plan.node("n1s1"))

    assert "bootstrap" in leader
    assert "bootstrap" not in standby
    assert leader["bootstrap"]["dcs"]["slots"] == {"n1s1": {"type": "physical"}}


def test_spock_gucs_reach_the_dcs(deployed_plan):
    parameters = pm.build_config(deployed_plan, deployed_plan.node("n1")) \
        ["bootstrap"]["dcs"]["postgresql"]["parameters"]

    assert parameters["wal_level"] == "logical"
    assert "spock" in parameters["shared_preload_libraries"]


def test_ttl_exceeds_loop_wait_plus_retry_timeout():
    """Otherwise a healthy leader loses its lease on a slow etcd round-trip."""
    assert pm.DEFAULT_TTL > pm.DEFAULT_LOOP_WAIT + pm.DEFAULT_RETRY_TIMEOUT


def test_rendered_yaml_round_trips(deployed_plan):
    import yaml

    text = pm.render_yaml(pm.build_config(deployed_plan, deployed_plan.node("n1")))

    assert text.startswith("# Managed by")
    assert yaml.safe_load(text)["scope"] == "pgedge-n1"


# ---------------------------------------------------------------------------
# waiting for a role
# ---------------------------------------------------------------------------


def systemd_replies(**extra):
    replies = {
        "is-system-running": (True, "running"),
        "systemctl is-active": (True, "active"),
        "NRestarts": (True, "0"),
    }
    replies.update(extra)
    return replies


def test_wait_returns_as_soon_as_the_node_leads():
    executor = FakeExecutor(systemd_replies(**{
        "list -f json": (True, members_json(("n1", "Leader", "running"))),
    }))

    ok, role, detail = pm.wait_for_role(executor, a_node(), ["Leader"], timeout=60)

    assert (ok, role) == (True, "Leader")
    assert "running" in detail


def test_wait_accepts_a_streaming_replica_for_a_standby():
    executor = FakeExecutor(systemd_replies(**{
        "list -f json": (True, members_json(("n1", "Replica", "streaming"))),
    }))

    ok, role, _ = pm.wait_for_role(executor, a_node(), ["Replica", "Sync Standby"],
                                   timeout=60)

    assert (ok, role) == (True, "Replica")


def test_a_dead_unit_fails_immediately():
    executor = FakeExecutor(systemd_replies(**{
        "systemctl is-active": (True, "failed"),
        "list -f json": (True, "[]"),
    }))

    ok, _, detail = pm.wait_for_role(executor, a_node(), ["Leader"], timeout=420)

    assert ok is False
    assert "patroni is not running" in detail
    assert executor.count("sleep 5") <= 1     # did not wait out the timeout


def test_a_restart_loop_is_not_mistaken_for_starting_up():
    """`activating` looks alive; a climbing restart count does not."""
    restarts = {"n": 0}

    class Looping(FakeExecutor):
        def _answer(self, command):
            self.commands.append(command)
            if "NRestarts" in command:
                restarts["n"] += 1
                return True, str(restarts["n"])
            if "is-system-running" in command:
                return True, "running"
            if "systemctl is-active" in command:
                return True, "activating"
            if "list -f json" in command:
                return True, "[]"
            if "curl" in command:
                return False, ""
            return True, ""

    ok, _, detail = pm.wait_for_role(Looping(), a_node(), ["Leader"], timeout=420)

    assert ok is False
    assert "restarted 3 times" in detail


def test_a_node_following_someone_else_gives_up_quickly():
    """A leftover standby holding the lock will never hand it over."""
    executor = FakeExecutor(systemd_replies(**{
        "list -f json": (True, members_json(("n1", "Replica", "streaming"),
                                            ("n1s1", "Leader", "running"))),
    }))
    logger = RecordingLogger()

    ok, _, detail = pm.wait_for_role(executor, a_node(), ["Leader"], timeout=420,
                                     run_logger=logger)

    assert ok is False
    assert "already led by n1s1" in detail
    assert "--cleanup" in detail
    assert executor.count("sleep 5") < 10


def test_rest_api_distinguishes_up_but_not_registered():
    executor = FakeExecutor(systemd_replies(**{
        "list -f json": (True, "[]"),
        "curl": (True, '{"state":"running","role":"master"}'),
    }))

    ok, _, detail = pm.wait_for_role(executor, a_node(), ["Leader"], timeout=30)

    assert ok is False
    assert "has not reached the DCS" in detail


def test_patronictl_errors_are_surfaced_not_swallowed():
    executor = FakeExecutor(systemd_replies(**{
        "list -f json": (True, ""),
        "curl": (False, ""),
        "list 2>&1": (True, "Error: Can not find suitable configuration"),
    }))

    ok, _, detail = pm.wait_for_role(executor, a_node(), ["Leader"], timeout=30)

    assert ok is False
    assert "Can not find suitable configuration" in detail


# ---------------------------------------------------------------------------
# service inspection
# ---------------------------------------------------------------------------


def test_is_running_reads_systemd_state():
    alive, detail = pm.is_running(
        FakeExecutor({"is-system-running": (True, "running"),
                      "systemctl is-active": (True, "activating")}), a_node())
    assert alive is True and "activating" in detail

    dead, detail = pm.is_running(
        FakeExecutor({"is-system-running": (True, "running"),
                      "systemctl is-active": (True, "failed")}), a_node())
    assert dead is False and "failed" in detail


def test_is_running_falls_back_to_pgrep_without_systemd():
    executor = FakeExecutor({"is-system-running": (False, ""), "pgrep": (True, "")})

    alive, detail = pm.is_running(executor, a_node())

    assert alive is True
    assert "patroni process running" == detail


def test_restart_count_parses_systemd_output():
    assert pm.restart_count(FakeExecutor({"NRestarts": (True, "4")}), "n1") == 4
    assert pm.restart_count(FakeExecutor({"NRestarts": (True, "")}), "n1") == 0
    assert pm.restart_count(FakeExecutor({"NRestarts": (True, "n/a")}), "n1") == 0


def test_unit_discovery_and_naming():
    executor = FakeExecutor({"list-unit-files": (
        True, "patroni-n1.service\npatroni-n2.service\npatroni-n2s1.service\n")})

    assert pm.installed_units(executor) == [
        "patroni-n1.service", "patroni-n2.service", "patroni-n2s1.service"]
    assert pm.unit_node_name("patroni-n2s1.service") == "n2s1"
    assert pm.unit_node_name("patroni-n2") == "n2"


def test_remove_instance_deletes_unit_config_and_data():
    executor = FakeExecutor()

    pm.remove_instance(executor, "n2s1", data_root="/var/lib/pgedge")

    for fragment in ("systemctl stop patroni-n2s1",
                     "systemctl disable patroni-n2s1",
                     "rm -f /etc/systemd/system/patroni-n2s1.service",
                     "rm -f /etc/patroni/n2s1.yml",
                     "rm -rf /var/lib/pgedge/n2s1",
                     "daemon-reload"):
        assert executor.ran(fragment), fragment


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_port_conflict_reports_a_foreign_listener():
    executor = FakeExecutor({
        "is-system-running": (True, "running"),
        "systemctl is-active": (True, "inactive"),
        "8008": (True, "LISTEN 0 244 0.0.0.0:8008 0.0.0.0:*"),
    })

    message = pm.port_conflict(executor, a_node())

    assert "patroni REST port 8008" in message
    assert "--base-restapi-port" in message


def test_port_conflict_ignores_this_nodes_own_patroni():
    executor = FakeExecutor({"is-system-running": (True, "running"),
                             "systemctl is-active": (True, "active")})

    assert pm.port_conflict(executor, a_node()) == ""


def test_check_binaries_names_what_the_db_user_cannot_run(deployed_plan):
    node = deployed_plan.node("n1")
    executor = FakeExecutor({
        "/usr/bin/patroni --version": (
            False, "bash: /usr/local/bin/patroni: bad interpreter: Permission denied"),
    })

    problems = pm.check_binaries(executor, deployed_plan, node)

    assert any("cannot run" in p and "Permission denied" in p for p in problems)


def test_check_binaries_is_quiet_when_both_run(deployed_plan):
    assert pm.check_binaries(FakeExecutor(), deployed_plan,
                             deployed_plan.node("n1")) == []


def test_permission_trace_walks_the_whole_chain():
    executor = FakeExecutor({"walk()": (
        True, "drwxr-xr-x /opt\ndrwx------ /opt/pgedge\nselinux: Enforcing")})

    trace = pm.permission_trace(executor, "/usr/local/bin/patroni")

    assert "drwx------ /opt/pgedge" in trace
    assert "selinux: Enforcing" in trace


def test_validate_config_runs_as_the_database_user(deployed_plan):
    executor = FakeExecutor({"--validate-config": (
        False, "restapi.connect_address ... must not contain \"localhost\"")})

    problem = pm.validate_config(executor, deployed_plan, deployed_plan.node("n1"))

    assert "must not contain" in problem
    assert executor.ran("--validate-config /etc/patroni/n1.yml")


def test_validate_config_is_silent_when_the_config_is_good(deployed_plan):
    assert pm.validate_config(FakeExecutor(), deployed_plan,
                              deployed_plan.node("n1")) == ""


def test_service_unit_runs_as_the_database_user(deployed_plan):
    executor = FakeExecutor()

    path = pm.write_service_unit(executor, deployed_plan, deployed_plan.node("n1"))

    unit = executor.written[path]
    assert "User=postgres" in unit
    assert "/etc/patroni/n1.yml" in unit
    assert "WorkingDirectory=/tmp" in unit


def test_cluster_status_summarises_a_scope():
    executor = FakeExecutor({"list -f json": (
        True, members_json(("n1", "Leader", "running"),
                           ("n1s1", "Replica", "streaming")))})

    status = pm.cluster_status(executor, a_node())

    assert status["leader"] == "n1"
    assert status["member_count"] == 2
    assert status["healthy"] is True
