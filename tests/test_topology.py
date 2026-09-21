#!/usr/bin/env python3
"""Placement: which node lands where, on which port, in which scope."""

import pytest

from aspects.cluster_model import Host
from deployment import topology


def test_one_node_per_host_when_hosts_suffice(three_hosts):
    plan, warnings = topology.plan_cluster(three_hosts, "pgedge", node_count=3)

    assert [n.host for n in plan.spock_nodes] == ["a", "b", "c"]
    assert {n.pg_port for n in plan.spock_nodes} == {5432}
    assert warnings == []


def test_surplus_nodes_share_a_host_on_distinct_ports(local_host):
    plan, warnings = topology.plan_cluster(local_host, "pgedge", node_count=3)

    assert [(n.name, n.pg_port, n.restapi_port) for n in plan.nodes] == [
        ("n1", 5432, 8008), ("n2", 5433, 8009), ("n3", 5434, 8010),
    ]
    assert any("share a failure domain" in w for w in warnings)


def test_ports_are_counted_per_machine_not_per_inventory_entry():
    """Two entries, one box: the ports must still differ.

    This is the failure that made a first deployment hang — both nodes claimed
    5432 and 8008 because the counter keyed on the inventory name.
    """
    hosts = [Host(name="na", address="localhost"),
             Host(name="nb", address="localhost")]

    plan, warnings = topology.plan_cluster(hosts, "pgedge", node_count=2)

    assert [(n.host, n.pg_port, n.restapi_port) for n in plan.nodes] == [
        ("na", 5432, 8008), ("nb", 5433, 8009),
    ]
    assert any("all point at localhost" in w for w in warnings)


def test_machine_key_distinguishes_ssh_ports():
    same = Host(name="a", address="10.0.0.1", port=22)
    other = Host(name="b", address="10.0.0.1", port=2222)

    assert topology.machine_key(same) != topology.machine_key(other)
    assert topology.machine_key(same) == topology.machine_key(
        Host(name="c", address="10.0.0.1")
    )


def test_base_ports_are_honoured(local_host):
    plan, _ = topology.plan_cluster(local_host, "pgedge", node_count=2,
                                    base_pg_port=6432, base_restapi_port=8108)

    assert [n.pg_port for n in plan.nodes] == [6432, 6433]
    assert [n.restapi_port for n in plan.nodes] == [8108, 8109]


def test_standby_prefers_a_different_machine(three_hosts):
    plan, warnings = topology.plan_cluster(three_hosts, "pgedge", node_count=2,
                                           standby_of=["n1"])

    standby = plan.node("n1s1")
    assert standby.host != plan.node("n1").host
    assert standby.leader == "n1"
    assert standby.scope == plan.node("n1").scope   # shares its leader's scope
    assert not any("shares" in w for w in warnings)


def test_standby_on_one_machine_is_reported_as_not_ha():
    """Two entries for one box must not look like two failure domains."""
    hosts = [Host(name="na", address="localhost"),
             Host(name="nb", address="localhost")]

    plan, warnings = topology.plan_cluster(hosts, "pgedge", node_count=2,
                                           standby_of=["n1"])

    assert plan.node("n1s1").address == plan.node("n1").address
    assert any("only one machine is available" in w for w in warnings)


def test_every_spock_node_leads_its_own_scope(three_hosts):
    plan, _ = topology.plan_cluster(three_hosts, "pgedge", node_count=3,
                                    standby_of=["n1"])

    assert plan.node("n1").scope == "pgedge-n1"
    assert plan.node("n2").scope == "pgedge-n2"
    assert plan.node("n1s1").scope == "pgedge-n1"
    assert plan.node("n1").standbys == ["n1s1"]


def test_unknown_standby_target_is_rejected(three_hosts):
    with pytest.raises(topology.TopologyError, match="unknown node"):
        topology.plan_cluster(three_hosts, "pgedge", node_count=2,
                              standby_of=["n7"])


def test_no_hosts_is_rejected():
    with pytest.raises(topology.TopologyError, match="no hosts"):
        topology.plan_cluster([], "pgedge", node_count=1)


def test_zero_nodes_is_rejected(local_host):
    with pytest.raises(topology.TopologyError, match="at least 1"):
        topology.plan_cluster(local_host, "pgedge", node_count=0)


def test_etcd_reserved_ports_are_refused(local_host):
    with pytest.raises(topology.TopologyError, match="etcd reserves"):
        topology.plan_cluster(local_host, "pgedge", node_count=1,
                              base_pg_port=2379)


def test_layout_is_deterministic(three_hosts):
    first, _ = topology.plan_cluster(three_hosts, "pgedge", node_count=5,
                                     standby_of=["n1", "n2"])
    again, _ = topology.plan_cluster(
        [Host(name=h.name, address=h.address) for h in three_hosts],
        "pgedge", node_count=5, standby_of=["n1", "n2"],
    )

    assert [(n.name, n.host, n.pg_port) for n in first.nodes] == \
           [(n.name, n.host, n.pg_port) for n in again.nodes]


def test_apply_platform_records_the_advertise_address(local_host):
    plan, _ = topology.plan_cluster(local_host, "pgedge", node_count=2)

    bin_dir = topology.apply_platform(plan, plan.hosts[0], {"family": "rhel"},
                                      advertise_address="192.168.5.20")

    assert bin_dir.endswith("/bin")
    assert plan.hosts[0].advertise_address == "192.168.5.20"
    assert all(n.advertise_address == "192.168.5.20" for n in plan.nodes)


def test_apply_platform_defaults_to_the_listed_address(three_hosts):
    plan, _ = topology.plan_cluster(three_hosts, "pgedge", node_count=1)

    topology.apply_platform(plan, plan.hosts[0], {"family": "rhel"})

    assert plan.hosts[0].advertise_address == "10.0.1.11"


def test_describe_standby_choice_says_whether_it_survives_host_loss(three_hosts):
    plan, _ = topology.plan_cluster(three_hosts, "pgedge", node_count=2,
                                    standby_of=["n1"])

    lines = topology.describe_standby_choice(plan)

    assert len(lines) == 1
    assert "survives host loss" in lines[0]
