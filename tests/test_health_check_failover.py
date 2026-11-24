#!/usr/bin/env python3
"""The failover health check: a scope is failed over and handed back.

What matters here is the sequence — seed, stop the leader, wait for the
promotion, look for the slots before touching the peers, retarget, write both
ways, restart the old leader, switch back — and that each refusal happens
before the step it protects: a standby that is not streaming means nothing is
stopped; slots missing after promotion means the peers are not retargeted at
a primary they could not connect to, and the old leader is still restarted.

The fake below is a tiny model of the scope: patronictl answers from `leader`
and `states`, systemctl and switchover move them, and the SQL the suite sends
is answered per member by port.
"""

import json
import re

import pytest

from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK, ROLE_STANDBY
from aspects.health_check import failover
from aspects.health_check import runner as regression
from tests.conftest import FakeExecutor, RecordingLogger

BIN = "/usr/pgsql-17/bin"


def node(name, port, host="local", role=ROLE_SPOCK, leader=None, scope=None):
    return Node(name=name, role=role, host=host, address=f"10.0.1.{port % 100}",
                pg_port=port, restapi_port=8000 + port % 100,
                data_dir=f"/var/lib/pgedge/{name}",
                scope=scope or f"pgedge-{name}", config_file=f"/etc/patroni/{name}.yml",
                pgpass_file="p", bin_dir=BIN, leader=leader)


def plan_with_standby(auto_repair=False, peer_host="local"):
    """n1 with standby n1s1, peer n2 (optionally on another host)."""
    hosts = [Host(name="local", address="10.0.1.10")]
    if peer_host != "local":
        hosts.append(Host(name=peer_host, address="10.0.1.20"))
    plan = ClusterPlan(cluster_name="pgedge", pg_major="17", pg_version="17.11",
                       hosts=hosts, auto_repair_failover=auto_repair)
    n1 = node("n1", 5432)
    n1.standbys = ["n1s1"]
    plan.nodes = [
        n1,
        node("n2", 5433, host=peer_host),
        node("n1s1", 5434, role=ROLE_STANDBY, leader="n1", scope="pgedge-n1"),
    ]
    return plan


class Scope(FakeExecutor):
    """A scope of two members and one peer, answering what the suite asks.

    `slots` is {member: (count, temporary)} as the member reports its spk_*
    slots. `promoted_slots` overrides what the promoted member reports once
    it leads — None keeps the standby's own figure, which is the healthy
    case. Rows are counted per phase as the suite inserts them, so a count
    query is answered with what was written.
    """

    PORTS = {5432: "n1", 5434: "n1s1", 5433: "n2"}

    def __init__(self, slots=None, promoted_slots=None, standby_state="streaming",
                 peer_status="replicating", callback_repairs=True, **kwargs):
        super().__init__(**kwargs)
        self.leader = "n1"
        self.states = {"n1": "running", "n1s1": standby_state}
        self.slots = dict(slots or {"n1": (1, 0), "n1s1": (1, 0)})
        self.promoted_slots = promoted_slots
        self.peer_status = peer_status
        self.callback_repairs = callback_repairs
        self.rows = {}
        self.retargeted = False
        self.events = []

    # --- the model ------------------------------------------------------
    def _members(self):
        out = []
        for name in ("n1", "n1s1"):
            state = self.states[name]
            if state == "stopped":
                continue
            role = "Leader" if name == self.leader else "Replica"
            out.append({"Member": name, "Host": "10.0.1.10", "Role": role,
                        "State": state, "TL": 1})
        return json.dumps(out)

    def _port(self, command):
        match = re.search(r"-p (\d+)", command)
        return self.PORTS.get(int(match.group(1))) if match else None

    def _answer(self, command):
        self.commands.append(command)
        return self._reply(command)

    def _reply(self, command):
        plain = command.replace("'\"'\"'", "'")

        if "list -f json" in command:
            return True, self._members()
        if "systemctl stop" in command:
            name = re.search(r"patroni-(\w+)", command).group(1)
            self.events.append(f"stop {name}")
            self.states[name] = "stopped"
            if self.leader == name:
                other = "n1s1" if name == "n1" else "n1"
                if self.states[other] == "streaming":
                    self.leader = other
                    self.states[other] = "running"
                    self.retargeted = self.callback_repairs
            return True, ""
        if "systemctl enable --now" in command:
            name = re.search(r"patroni-(\w+)", command).group(1)
            self.events.append(f"start {name}")
            self.states[name] = "streaming" if self.leader != name else "running"
            return True, ""
        if " switchover " in command:
            candidate = re.search(r"--candidate (\w+)", command).group(1)
            self.events.append(f"switchover {candidate}")
            old = self.leader
            self.leader = candidate
            self.states[candidate] = "running"
            self.states[old] = "streaming"
            self.retargeted = self.callback_repairs
            return True, "Successfully switched over"

        member = self._port(command)
        if "pg_is_in_recovery" in plain:
            return True, "f\n" if member == self.leader or member == "n2" else "t\n"
        if "pg_replication_slots" in plain:
            figure = self.slots.get(member, (0, 0))
            if member == self.leader and member != "n1" and self.promoted_slots:
                # what the promoted standby reports once it leads
                figure = ((0, 0) if self.promoted_slots == "none"
                          else self.promoted_slots)
            return True, f"{figure[0]}|{figure[1]}\n"
        if "sub_show_status" in plain:
            status = self.peer_status if self.retargeted else "down"
            return True, f"sub_n1_n2|{status}|n1\n"
        if "sub_alter_interface" in plain:
            self.events.append("retarget n2")
            self.retargeted = True
            return True, ""
        if "INSERT INTO failover_regress.rows" in plain:
            phase = re.search(r"'([^']+)' *\n?\s*FROM generate_series\(1, (\d+)\)", plain) \
                or re.search(r"g(?: \+ \d+)?, '([^']+)' FROM generate_series\(1, (\d+)\)", plain)
            self.rows[phase.group(1)] = self.rows.get(phase.group(1), 0) + int(phase.group(2))
            return True, ""
        if "DELETE FROM failover_regress.rows" in plain:
            self.rows = {}
            return True, ""
        if "count(*) FROM failover_regress.rows WHERE phase" in plain:
            phase = re.search(r"phase = '([^']+)'", plain).group(1)
            return True, f"{self.rows.get(phase, 0)}\n"
        if "pg_namespace" in plain:
            return True, "0\n"
        if "FROM spock.tables" in plain:
            return True, "1\n"
        return FakeExecutor._answer(self, command)


class OtherHost(FakeExecutor):
    """A second host's executor: its own command log, the same scope."""

    def __init__(self, scope):
        super().__init__(host="other")
        self.scope = scope

    def _answer(self, command):
        self.commands.append(command)
        return self.scope._reply(command)


@pytest.fixture(autouse=True)
def simulated_clock(monkeypatch):
    """Every wait advances a clock of its own instead of sleeping, so a wait
    that never ends times out at once rather than after five real minutes."""
    clock = {"t": 0.0}
    monkeypatch.setattr(failover, "_now", lambda: clock["t"])
    monkeypatch.setattr(failover, "_sleep",
                        lambda seconds: clock.__setitem__("t", clock["t"] + seconds))


def run(plan, executor, tests=None, log=None, timeout=None):
    suite = failover.FailoverSuite(tests=tests, test_timeout=timeout)
    return suite.run(executor, plan, plan.node("n1"), "in-tree", run_logger=log)


def verdicts(result):
    return dict(re.findall(r"^test (\S+)\s+\.\.\. (\S+)", result.output, re.M))


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def test_failover_runs_by_name_only():
    """It stops a leader: a run of 'all' must not do that unasked."""
    assert "failover" in regression.known_components()
    assert "failover" not in regression.default_components()
    assert regression.parse_components("failover") == ["failover"]
    assert regression.suite_for("failover").targets_cluster is True


def test_without_a_standby_it_is_skipped_naming_the_command_that_adds_one():
    plan = plan_with_standby()
    plan.nodes = [n for n in plan.nodes if n.name != "n1s1"]
    plan.node("n1").standbys = []

    result = run(plan, Scope())

    assert result.status == regression.SKIPPED
    assert "add-standby --leader n1" in result.detail
    assert not any("systemctl" in c for c in Scope().commands)


def test_with_one_spock_node_there_is_no_peer_to_replicate_with():
    plan = plan_with_standby()
    plan.nodes = [n for n in plan.nodes if n.name != "n2"]

    result = run(plan, Scope())

    assert result.status == regression.SKIPPED
    assert "peer" in result.detail


# ---------------------------------------------------------------------------
# the healthy path
# ---------------------------------------------------------------------------


def test_the_scope_is_failed_over_checked_and_handed_back():
    plan = plan_with_standby(auto_repair=False)
    scope = Scope()
    log = RecordingLogger()

    result = run(plan, scope, log=log)

    assert result.status == regression.PASSED, result.output
    assert result.tests == {"total": 4, "passed": 4, "failed": 0}
    # stop, promotion, restart the old leader, then repair by hand; switch
    # back and repair again (every promotion moves the subject). The restart
    # comes before any write: a strict synchronous scope holds commits until
    # its standby is back.
    assert scope.events == ["stop n1", "start n1", "retarget n2",
                            "switchover n1", "retarget n2"]
    assert scope.leader == "n1", "the scope ends led by its Spock node"
    assert scope.states == {"n1": "running", "n1s1": "streaming"}
    for line in ("standby_slots_permanent|true", "standby_promoted|true",
                 "new_primary_has_spock_slots|true",
                 "peers_retargeted_by_hand|true", "no_data_loss|true",
                 "forward_replication|true", "reverse_replication|true",
                 "old_leader_rejoined|true", "switched_over|true",
                 "scope_led_by_its_spock_node|true", "schema_gone|true"):
        assert line in result.output, line
    assert log.said("[2/4] 02_unplanned_failover")


def test_nothing_is_written_to_the_new_primary_before_the_old_leader_is_back():
    """In strict synchronous mode the promoted member holds every commit
    until a standby streams again; writing first would hang the case."""
    plan = plan_with_standby()
    scope = Scope()

    run(plan, scope, tests=["02_unplanned_failover"])

    restarted = next(i for i, c in enumerate(scope.commands)
                     if "systemctl enable --now" in c)
    written = [i for i, c in enumerate(scope.commands)
               if "INSERT INTO failover_regress.rows" in c and "-p 5434" in c]
    assert written and min(written) > restarted


def test_the_seed_rows_go_in_before_the_leader_is_stopped():
    plan = plan_with_standby()
    scope = Scope()

    run(plan, scope, tests=["02_unplanned_failover"])

    seeded = next(i for i, c in enumerate(scope.commands)
                  if "INSERT INTO failover_regress.rows" in c)
    stopped = next(i for i, c in enumerate(scope.commands)
                   if "systemctl stop" in c)
    assert seeded < stopped


def test_the_peers_are_pointed_at_the_promoted_members_port():
    """The retarget goes through the same interface naming the callback
    uses, at the promoted member's address and port, so a cluster that is
    later repaired by the callback finds the interface already there."""
    plan = plan_with_standby(auto_repair=False)
    scope = Scope()

    run(plan, scope, tests=["02_unplanned_failover"])

    retarget = [c for c in scope.commands if "node_add_interface" in c]
    assert retarget and "n1_promoted_5434" in retarget[0]
    assert "port=5434" in retarget[0]
    assert "-p 5433" in retarget[0], "issued on the peer"


def test_with_auto_repair_on_the_callback_is_waited_for_not_replaced():
    plan = plan_with_standby(auto_repair=True)
    scope = Scope(callback_repairs=True)

    result = run(plan, scope, tests=["02_unplanned_failover"])

    assert result.status == regression.PASSED, result.output
    assert "peers_retargeted_by_callback|true" in result.output
    assert not any("sub_alter_interface" in c for c in scope.commands)


def test_a_callback_that_does_not_fire_is_reported_and_the_peers_repaired():
    plan = plan_with_standby(auto_repair=True)
    scope = Scope(callback_repairs=False)

    result = run(plan, scope, tests=["02_unplanned_failover"], timeout=1)

    assert "peers_retargeted_by_callback|false" in result.output
    assert "peers_retargeted_by_hand|true" in result.output
    assert any("sub_alter_interface" in c for c in scope.commands)


def test_the_suite_touches_only_its_own_schema():
    plan = plan_with_standby()
    scope = Scope()

    run(plan, scope)

    for command in scope.commands:
        for verb in ("CREATE TABLE", "INSERT INTO", "DELETE FROM", "DROP SCHEMA"):
            if verb in command:
                assert "failover_regress" in command, command
    assert any("DROP SCHEMA IF EXISTS failover_regress CASCADE" in c
               for c in scope.commands)


# ---------------------------------------------------------------------------
# what the failover reveals
# ---------------------------------------------------------------------------


def test_a_temporary_slot_copy_on_the_standby_fails_the_first_case():
    """The reported 5.0.14 symptom: the copy is there but temporary, so a
    promotion would drop it. Found before anything is failed over, and the
    failover still runs so the consequence is on record too."""
    plan = plan_with_standby()
    scope = Scope(slots={"n1": (1, 0), "n1s1": (1, 1)})

    result = run(plan, scope)

    assert verdicts(result)["01_standby_slots"] == "FAILED"
    assert "standby_slots_permanent|false" in result.output
    assert "temporary" in result.output
    assert "stop n1" in scope.events, "the failover was still exercised"


def test_slots_never_copied_to_the_standby_is_its_own_finding():
    plan = plan_with_standby()
    scope = Scope(slots={"n1": (1, 0), "n1s1": (0, 0)})

    result = run(plan, scope, tests=["01_standby_slots"], timeout=1)

    assert result.status == regression.FAILED
    assert "0 of 1 spk_* slots" in result.output
    assert "a promotion would lose them" in result.output


def test_slots_lost_at_promotion_stop_the_case_before_the_peers_are_retargeted():
    """Retargeting peers at a primary without slots would only produce the
    'replication slot does not exist' loop; the finding is the missing slot.
    The old leader is restarted regardless, and the scope is handed back."""
    plan = plan_with_standby(auto_repair=False)
    scope = Scope(promoted_slots="none")

    result = run(plan, scope)

    assert verdicts(result)["02_unplanned_failover"] == "FAILED"
    assert "new_primary_has_spock_slots|false" in result.output
    assert "did not survive" in result.output
    # no retarget between the stop and the restart: the peers were not
    # pointed at a primary without slots
    assert scope.events[:2] == ["stop n1", "start n1"]
    assert scope.leader == "n1"


def test_a_leader_whose_standby_is_not_streaming_is_left_alone():
    plan = plan_with_standby()
    scope = Scope(standby_state="stopped")

    result = run(plan, scope, tests=["02_unplanned_failover"])

    assert result.status == regression.FAILED
    assert "nothing was stopped" in result.output
    assert not any("systemctl stop" in c for c in scope.commands)


def test_a_promotion_that_never_comes_is_reported_and_the_leader_restarted():
    plan = plan_with_standby()

    class Stuck(Scope):
        def _answer(self, command):
            ok, output = super()._answer(command)
            if "systemctl stop" in command:
                self.leader = "n1"          # the DCS never moves
                self.states["n1"] = "stopped"
            return ok, output

    scope = Stuck()
    result = run(plan, scope, tests=["02_unplanned_failover"], timeout=1)

    assert "promotion of n1s1 not within 1s" in result.output
    assert "start n1" in scope.events


# ---------------------------------------------------------------------------
# members on other hosts
# ---------------------------------------------------------------------------


def test_a_peer_on_another_host_is_reached_through_its_own_executor():
    plan = plan_with_standby(peer_host="other")
    local = Scope()
    remote = OtherHost(local)
    suite = failover.FailoverSuite(executors={"other": remote})

    result = suite.run(local, plan, plan.node("n1"), "in-tree")

    assert result.status == regression.PASSED, result.output
    assert any("-p 5433" in c and "count(*)" in c for c in remote.commands)
    assert not any("-p 5433" in c and "count(*)" in c for c in local.commands)


def test_the_scenarios_are_counted_like_the_other_in_tree_cases():
    suite = failover.FailoverSuite()
    output = ("test 01_standby_slots            ... ok\n"
              "  standby_slots_permanent|true\n"
              "test 02_unplanned_failover       ... FAILED — promotion of n1s1 "
              "not within 300s\n")
    assert suite.parse(output) == {"total": 2, "passed": 1, "failed": 1}


# ---------------------------------------------------------------------------
# a Spock 6 standby on PostgreSQL 17+ with native slot sync off
# ---------------------------------------------------------------------------


def test_no_copies_on_a_spock6_standby_names_the_setting_that_makes_them():
    """On 18 Spock 6 registers no worker of its own, so a standby without
    sync_replication_slots has no copies and never will. The verdict says
    so and names the command, rather than blaming a worker that is not
    there."""
    plan = plan_with_standby()
    plan.spock_major = "60"
    plan.pg_version = "18.7"
    scope = Scope(slots={"n1": (1, 0), "n1s1": (0, 0)},
                  replies={"SHOW sync_replication_slots": (True, "off\n")})

    result = run(plan, scope, tests=["01_standby_slots"], timeout=1)

    assert "sync_replication_slots is off on n1s1" in result.output
    assert "db guc-set sync_replication_slots on" in result.output


def test_a_spock5_standby_without_copies_is_not_told_about_native_sync():
    plan = plan_with_standby()
    plan.spock_major = "50"
    plan.pg_version = "18.7"
    scope = Scope(slots={"n1": (1, 0), "n1s1": (0, 0)})

    result = run(plan, scope, tests=["01_standby_slots"], timeout=1)

    assert "nothing has copied them" in result.output
    assert "sync_replication_slots" not in result.output
