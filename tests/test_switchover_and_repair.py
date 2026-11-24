#!/usr/bin/env python3
"""What a live four-node cluster taught the tool in one afternoon.

A switchover to a member that had rejoined seconds earlier promoted it with
no spk_* slots; the member it demoted then dropped its own copies as
surplus, and the scope's outbound replication was gone. `--repair-failover`
looked at where the leader was, saw it at home, and said nothing was wrong
while every peer still pointed at the standby. The ACE check could not find
a config file that was one directory up. `service logs --component postgres`
printed Patroni's journal. And a stalled SSH channel sat for the whole
600-second command timeout before anyone noticed. Each is pinned here.
"""

import json

import pytest

from aspects import cluster_services, patroni_management, spock_management
from aspects import ssh_executor
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK, ROLE_STANDBY
from aspects.health_check import failover
from aspects.health_check import runner as regression
from deployment import cli
from tests.conftest import FakeExecutor, RecordingLogger
from tests.test_health_check_failover import Scope, plan_with_standby, verdicts

BIN = "/usr/pgsql-17/bin"


def node(name, port, role=ROLE_SPOCK, leader=None, scope=None):
    return Node(name=name, role=role, host="local", address="10.0.1.10",
                pg_port=port, restapi_port=8000 + port % 100,
                data_dir=f"/var/lib/pgedge/{name}", scope=scope or f"pgedge-{name}",
                config_file=f"/etc/patroni/{name}.yml", pgpass_file="p",
                bin_dir=BIN, leader=leader)


def a_plan():
    n1 = node("n1", 5432)
    n1.standbys = ["n1s1"]
    return ClusterPlan(
        cluster_name="pgedge", hosts=[Host(name="local", address="10.0.1.10")],
        nodes=[n1, node("n2", 5433), node("n3", 5435),
               node("n1s1", 5434, role=ROLE_STANDBY, leader="n1", scope="pgedge-n1")])


# ---------------------------------------------------------------------------
# 1. a switchover to a member without the slot copies is refused
# ---------------------------------------------------------------------------


def test_a_candidate_without_slot_copies_is_refused():
    plan = a_plan()
    executor = FakeExecutor(replies={"pg_replication_slots": (True, "0|0\n")})

    ok, message = cluster_services.switchover(
        executor, plan, plan.node("n1"), "n1s1",
        executor_for=lambda n: executor)

    assert ok is False
    assert "0 of 2 spk_* slot copies" in message
    assert "--force" in message
    assert not executor.ran("patronictl") and not executor.ran("switchover")


def test_a_temporary_copy_counts_as_missing():
    plan = a_plan()
    executor = FakeExecutor(replies={"pg_replication_slots": (True, "2|1\n")})

    ok, message = cluster_services.switchover(
        executor, plan, plan.node("n1"), "n1s1",
        executor_for=lambda n: executor)

    assert ok is False and "1 temporary" in message


def test_a_candidate_with_every_copy_is_switched_to():
    plan = a_plan()
    executor = FakeExecutor(replies={"pg_replication_slots": (True, "2|0\n"),
                                     "switchover": (True, "Successfully switched over")})

    ok, _ = cluster_services.switchover(
        executor, plan, plan.node("n1"), "n1s1",
        executor_for=lambda n: executor)

    assert ok is True
    assert any("switchover pgedge-n1 --candidate n1s1" in c for c in executor.commands)


def test_force_skips_the_check():
    plan = a_plan()
    executor = FakeExecutor(replies={"pg_replication_slots": (True, "0|0\n"),
                                     "switchover": (True, "ok")})

    ok, _ = cluster_services.switchover(
        executor, plan, plan.node("n1"), "n1s1",
        executor_for=lambda n: executor, force=True)

    assert ok is True


def test_the_failover_suite_waits_for_the_copies_before_switching_back(monkeypatch):
    """The exact sequence that emptied a live scope: unplanned failover,
    old leader rejoins, switch back at once. The suite now waits for the
    rejoined member to carry the copies, and with none arriving it refuses
    the switch rather than making the scope slot-less."""
    plan = plan_with_standby()
    scope = Scope(slots={"n1": (0, 0), "n1s1": (1, 0)})

    suite = failover.FailoverSuite(tests=["03_switchover_back"], test_timeout=1)
    result = suite.run(scope, plan, plan.node("n1"), "in-tree")

    assert verdicts(result)["03_switchover_back"] == "FAILED"
    assert "candidate_carries_spock_slots|false" in result.output
    assert "no switchover was attempted" in result.output
    # it switched to n1s1 (which had the copies) and then stopped
    assert scope.events.count("switchover n1s1") == 1
    assert "switchover n1" not in scope.events


# ---------------------------------------------------------------------------
# 2. repair-failover asks the peers where they point
# ---------------------------------------------------------------------------


def members_json(leader="n1"):
    return json.dumps([
        {"Member": "n1", "Role": "Leader" if leader == "n1" else "Replica",
         "State": "running", "Host": "10.0.1.10:5432"},
        {"Member": "n1s1", "Role": "Leader" if leader == "n1s1" else "Replica",
         "State": "streaming", "Host": "10.0.1.10:5434"},
    ])


def repair(monkeypatch, plan, executor):
    monkeypatch.setattr(cli, "_executor_pool",
                        lambda plan, run_logger=None: (lambda n: executor, {}))
    log = RecordingLogger()
    monkeypatch.setattr(cli, "RunLogger", lambda run_id: log)
    code = cli._repair_failover(plan, "n1")
    return code, log


def test_peers_still_pointing_at_the_standby_are_retargeted_after_a_switch_back(monkeypatch):
    plan = a_plan()
    executor = FakeExecutor(replies={
        "list -f json": (True, members_json("n1")),
        "sub_origin_if": (True, "host=10.0.1.10 port=5434 dbname=postgres user=postgres\n"),
    })

    code, log = repair(monkeypatch, plan, executor)

    assert code == cli.EXIT_OK
    assert log.said("n1 leads pgedge-n1 again, but")
    assert log.said("n2 still points at 10.0.1.10:5434")
    retargets = [c for c in executor.commands if "sub_alter_interface" in c]
    assert len(retargets) == 2, "both peers moved"
    assert any("port=5432" in c for c in executor.commands if "node_add_interface" in c)


def test_a_scope_whose_peers_point_at_its_leader_is_left_alone(monkeypatch):
    plan = a_plan()
    executor = FakeExecutor(replies={
        "list -f json": (True, members_json("n1")),
        "sub_origin_if": (True, "host=10.0.1.10 port=5432 dbname=postgres user=postgres\n"),
    })

    code, log = repair(monkeypatch, plan, executor)

    assert code == cli.EXIT_OK
    assert log.said("every peer points at it. Nothing to do.")
    assert not executor.ran("sub_alter_interface")


def test_a_failed_over_scope_is_repaired_as_before(monkeypatch):
    plan = a_plan()
    executor = FakeExecutor(replies={
        "list -f json": (True, members_json("n1s1")),
        "sub_origin_if": (True, "host=10.0.1.10 port=5432 dbname=postgres user=postgres\n"),
    })

    code, log = repair(monkeypatch, plan, executor)

    assert code == cli.EXIT_OK
    assert log.said("has failed over: n1s1 now leads")
    assert any("port=5434" in c for c in executor.commands if "node_add_interface" in c)


def test_the_subscription_target_is_read_from_the_bound_interface():
    plan = a_plan()
    executor = FakeExecutor(replies={
        "sub_origin_if": (True, "host=10.0.1.10 port=5434 dbname=postgres user=postgres password=x\n"),
    })

    target = spock_management.subscription_target(
        executor, plan, plan.node("n2"), "sub_n1_n2")

    assert target == ("10.0.1.10", "5434")
    sql = next(c for c in executor.commands if "sub_origin_if" in c)
    assert "sub_n1_n2" in sql and "-p 5433" in sql, "asked on the peer"


# ---------------------------------------------------------------------------
# 3. ACE runs where its config is
# ---------------------------------------------------------------------------


def test_ace_runs_from_the_workspace_that_holds_its_config():
    """ACE reads ace.yaml from the working directory only; from the output
    subdirectory every check failed with 'config file not found'."""
    from tests.test_health_check_suites import plan_with, ready

    plan = plan_with()
    executor = ready(**{"command -v ace": (True, "")})

    regression.suite_for("ace").run(executor, plan, plan.node("n1"), "installed")

    runs = [c for c in executor.commands if "spock-diff" in c and "--dbname" in c]
    assert runs
    workspace = f"{regression.REGRESSION_ROOT}/ace"
    assert all(f"cd {workspace} &&" in c for c in runs)
    assert all(f"mv -f {workspace}/*.json {workspace}/output/" in c for c in runs)


# ---------------------------------------------------------------------------
# 5. service logs --component postgres reads the server's own log
# ---------------------------------------------------------------------------


def test_postgres_logs_come_from_the_servers_log_directory():
    plan = a_plan()
    n1 = plan.node("n1")
    executor = FakeExecutor(replies={
        "ls -t": (True, "LOG:  database system is ready to accept connections\n"),
    })

    text = cluster_services.logs(executor, plan, n1, component="postgres", lines=20)

    assert "ready to accept connections" in text
    command = next(c for c in executor.commands if "ls -t" in c)
    assert patroni_management.log_directory_for(n1) in command
    assert "tail -n 20" in command
    assert not executor.ran("journalctl")


def test_a_node_with_no_log_file_yet_falls_back_to_patronis_journal():
    plan = a_plan()
    executor = FakeExecutor(replies={
        "ls -t": (True, ""),
        "journalctl": (True, "patroni: starting postgres\n"),
    })

    text = cluster_services.logs(executor, plan, plan.node("n1"), component="postgres")

    assert "starting postgres" in text


# ---------------------------------------------------------------------------
# 4. a connection keeps itself alive
# ---------------------------------------------------------------------------


def test_the_transport_sends_keepalives(monkeypatch):
    """Without them a channel on a dropped connection blocks for the whole
    command timeout; with them paramiko notices within seconds and the
    retry loop reconnects."""
    calls = {}

    class Transport:
        def set_keepalive(self, seconds):
            calls["keepalive"] = seconds

        def is_active(self):
            return True

    class Client:
        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            calls["connect"] = kwargs

        def get_transport(self):
            return Transport()

        def close(self):
            pass

    monkeypatch.setattr(ssh_executor.paramiko, "SSHClient", Client)
    monkeypatch.delenv("PG_CLUSTER_SSH_KEY", raising=False)

    ssh_executor.SSHExecutor("n1", "10.0.1.10", "root").connect()

    assert calls["keepalive"] == ssh_executor.KEEPALIVE_SECONDS
    assert 0 < ssh_executor.KEEPALIVE_SECONDS <= 30
