#!/usr/bin/env python3
"""Whether a replication slot is in use is read from the server, not guessed.

The status page and the pre-removal lag wait both ask pg_replication_slots
for `active`. PostgreSQL prints a bare boolean as t/f and a boolean cast to
text as true/false; a parser that knows one spelling reads every slot as idle
under the other. That showed up as a healthy cluster reported DEGRADED with
every slot "inactive, retaining 56 bytes", and as a lag wait that returned at
once because no slot looked active. Both spellings are accepted now, and the
queries ask for the spelling the parser expects.
"""

import pytest

from aspects import health, spock_management, spock_operations
from aspects.cluster_model import ClusterPlan, Host, Node
from conftest import FakeExecutor


def a_plan():
    host = Host(name="local", address="10.0.1.10")
    n1 = Node(name="n1", role="spock", host="local", address="10.0.1.10",
              pg_port=5432, restapi_port=8008, data_dir="/d", scope="pgedge-n1",
              config_file="/c", pgpass_file="/p", bin_dir="/usr/pgsql-17/bin")
    n1.standbys = ["n1s1"]
    plan = ClusterPlan(cluster_name="pgedge", hosts=[host], nodes=[n1])
    return plan


@pytest.mark.parametrize("spelling", ["t", "true", "TRUE"])
def test_an_active_slot_is_read_as_active_whatever_the_server_prints(spelling):
    plan = a_plan()
    executor = FakeExecutor(replies={
        "FROM pg_replication_slots": (True, f"n1s1|physical|{spelling}|0 bytes\n"
                                            f"spk_postgres_n1_sub_n1_n2|logical|f|56 bytes\n"),
    })

    slots = spock_management.replication_slots(executor, plan, plan.node("n1"))

    assert [(s["name"], s["active"]) for s in slots] == [
        ("n1s1", True), ("spk_postgres_n1_sub_n1_n2", False)]


def test_the_slot_query_asks_for_the_spelling_the_parser_reads():
    """Spelled out in SQL rather than cast: `active::text` is 'true', which
    the parser used to compare with 't'."""
    plan = a_plan()
    executor = FakeExecutor()

    spock_management.replication_slots(executor, plan, plan.node("n1"))

    sql = next(c for c in executor.commands if "pg_replication_slots" in c)
    assert "active::text" not in sql
    assert "CASE WHEN active THEN" in sql


def test_a_healthy_node_is_not_reported_degraded_for_its_busy_slots():
    plan = a_plan()
    executor = FakeExecutor(replies={
        "pg_isready": (True, "accepting connections"),
        "SHOW server_version": (True, "18.7\n"),
        "pg_is_in_recovery": (True, "f\n"),
        "curl -fsS": (True, '{"role": "primary", "state": "running"}'),
        "FROM pg_replication_slots": (True, "n1s1|physical|t|0 bytes\n"
                                            "spk_postgres_n1_sub_n1_n2|logical|t|56 bytes\n"),
        "spock_version": (True, "6.0.0\n"),
        "FROM spock.node;": (True, "n1,n2\n"),
        "sub_show_status": (True, "sub_n2_n1|replicating|n2\n"),
    })
    plan.nodes.append(Node(name="n2", role="spock", host="local",
                           address="10.0.1.10", pg_port=5433, restapi_port=8009,
                           data_dir="/d", scope="pgedge-n2", config_file="/c",
                           pgpass_file="/p", bin_dir="/usr/pgsql-17/bin"))

    entry = health.collect_node(executor, plan, plan.node("n1"))

    assert not any("inactive replication slot" in p for p in entry["problems"]), \
        entry["problems"]
    assert entry["status"] == "ok"


def test_an_idle_slot_is_still_reported():
    plan = a_plan()
    executor = FakeExecutor(replies={
        "pg_isready": (True, ""),
        "pg_is_in_recovery": (True, "f\n"),
        "curl -fsS": (True, '{"role": "primary", "state": "running"}'),
        "FROM pg_replication_slots": (True, "spk_postgres_n1_sub_n1_n2|logical|f|3 GB\n"),
        "FROM spock.node;": (True, "n1\n"),
    })

    entry = health.collect_node(executor, plan, plan.node("n1"))

    assert any("spk_postgres_n1_sub_n1_n2 (3 GB)" in p for p in entry["problems"])


def test_the_lag_wait_waits_while_an_active_slot_is_behind(monkeypatch):
    """With `active` misread, no slot was ever active and the wait returned
    (True, 0) at once — so a node was removed with WAL still in flight."""
    monkeypatch.setattr(spock_operations.time, "sleep", lambda s: None)
    plan = a_plan()
    sep = "\x1f"
    executor = FakeExecutor(replies={
        "FROM pg_replication_slots": (
            True, sep.join(["spk_postgres_n1_sub_n1_n2", "logical", "t",
                            "4096", "4096 bytes"]) + "\n"),
    })

    drained, worst = spock_operations.wait_for_lag_below(
        executor, plan, plan.node("n1"), max_bytes=1024, timeout=10, interval=5)

    assert drained is False
    assert worst == 4096
    assert executor.count("FROM pg_replication_slots") == 2, "polled until the timeout"


def test_the_lag_wait_returns_once_the_active_slot_has_caught_up(monkeypatch):
    monkeypatch.setattr(spock_operations.time, "sleep", lambda s: None)
    plan = a_plan()
    sep = "\x1f"
    executor = FakeExecutor(replies={
        "FROM pg_replication_slots": (
            True, sep.join(["spk_postgres_n1_sub_n1_n2", "logical", "true",
                            "16", "16 bytes"]) + "\n"),
    })

    drained, worst = spock_operations.wait_for_lag_below(
        executor, plan, plan.node("n1"), max_bytes=1024, timeout=10, interval=5)

    assert (drained, worst) == (True, 16)
