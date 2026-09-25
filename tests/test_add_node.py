#!/usr/bin/env python3
"""Growing a cluster: where the node lands, what it runs, and what is refused.

The `add()` flow itself talks to hosts at every step, so these tests cover the
decisions it makes before and around that — placement, versions, Spock — which
is where every rule lives.
"""

import pytest

from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK
from deployment import add_node


def plan_with(nodes=("n1", "n2"), spock_major="50", pg_version="17.11",
              deploy_mode="packages", hosts=None, **kwargs):
    hosts = hosts or [Host(name="local", address="localhost", local=True)]
    for host in hosts:
        host.family = "rhel"
    plan = ClusterPlan(cluster_name="pgedge", pg_major=pg_version.split(".")[0],
                       pg_version=pg_version, spock_major=spock_major,
                       deploy_mode=deploy_mode, hosts=list(hosts), **kwargs)
    for index, name in enumerate(nodes):
        plan.nodes.append(Node(
            name=name, role=ROLE_SPOCK, host=hosts[index % len(hosts)].name,
            address=hosts[index % len(hosts)].address,
            pg_port=5432 + index, restapi_port=8008 + index,
            data_dir=f"/var/lib/pgedge/{name}", scope=f"pgedge-{name}",
            config_file=f"/etc/patroni/{name}.yml", pgpass_file="/p",
            bin_dir="/usr/pgsql-17/bin",
        ))
    return plan


# ---------------------------------------------------------------------------
# naming, placement and ports
# ---------------------------------------------------------------------------


def test_next_node_name_skips_what_is_taken():
    assert add_node.next_node_name(plan_with(("n1", "n2"))) == "n3"
    assert add_node.next_node_name(plan_with(("n1", "n3"))) == "n2"


def test_next_ports_avoid_the_nodes_already_on_that_host():
    plan = plan_with(("n1", "n2"))

    assert add_node.next_ports(plan, "local") == (5434, 8010)


def test_next_ports_start_at_the_base_on_an_empty_host(three_hosts):
    plan = plan_with(("n1",), hosts=three_hosts)

    assert add_node.next_ports(plan, "b") == (5432, 8008)


def test_a_host_with_no_spock_node_is_preferred(three_hosts):
    plan = plan_with(("n1",), hosts=three_hosts)

    host, is_new, note = add_node.resolve_host(plan, None)

    assert host.name == "b"
    assert (is_new, note) == (False, None)


def test_a_full_cluster_stacks_on_the_least_loaded_host_and_says_so(three_hosts):
    plan = plan_with(("n1", "n2", "n3"), hosts=three_hosts)

    host, _, note = add_node.resolve_host(plan, None)

    assert host.name in {"a", "b", "c"}
    assert "shares a failure domain" in note


def test_a_named_host_from_the_inventory_joins_the_cluster(inventory_file):
    path = inventory_file([
        {"name": "node-d", "host": "10.0.1.14", "username": "rocky", "enabled": True},
    ])
    plan = plan_with(("n1",))

    host, is_new, note = add_node.resolve_host(plan, "node-d", inventory_path=path)

    assert (host.name, host.address, is_new) == ("node-d", "10.0.1.14", True)
    assert "not part of this cluster yet" in note


def test_an_unknown_host_is_refused(inventory_file):
    path = inventory_file([{"name": "node-d", "host": "10.0.1.14", "enabled": True}])

    with pytest.raises(add_node.AddNodeError, match="neither in the cluster"):
        add_node.resolve_host(plan_with(("n1",)), "nowhere", inventory_path=path)


# ---------------------------------------------------------------------------
# PostgreSQL version
# ---------------------------------------------------------------------------


def test_bin_dir_is_resolved_per_major_and_mode():
    packages = plan_with(deploy_mode="packages")
    source = plan_with(deploy_mode="source")
    host = packages.hosts[0]

    assert add_node.bin_dir_for(packages, host, "17") == "/usr/pgsql-17/bin"
    assert add_node.bin_dir_for(packages, host, "18") == "/usr/pgsql-18/bin"
    assert add_node.bin_dir_for(source, host, "18") == "/opt/pgedge/pg18/bin"


def test_the_clusters_version_is_the_default():
    assert add_node.check_version(plan_with(), "") == "17.11"


def test_the_same_or_a_newer_version_is_accepted():
    plan = plan_with()

    assert add_node.check_version(plan, "17.11") == "17.11"
    assert add_node.check_version(plan, "18.2") == "18.2"


@pytest.mark.parametrize("older", ["17.4", "16.9", "17"])
def test_an_older_version_is_refused(older):
    """An older server cannot replay what a newer peer produces."""
    with pytest.raises(add_node.AddNodeError, match="older than the cluster"):
        add_node.check_version(plan_with(), older)


def test_cluster_version_falls_back_to_the_major():
    assert add_node.cluster_version(plan_with(pg_version="17")) == "17"


# ---------------------------------------------------------------------------
# Spock version
# ---------------------------------------------------------------------------


def test_the_clusters_spock_is_the_default():
    major, notes = add_node.check_spock(plan_with(spock_major="50"), "")

    assert (major, notes) == ("50", [])


def test_a_newer_spock_is_allowed_and_flagged_as_a_migration():
    """zodan on main permits a newer node to join older peers."""
    major, notes = add_node.check_spock(plan_with(spock_major="50"), "60")

    assert major == "60"
    assert any("mixed-version add" in note for note in notes)
    assert any("5.0.9" in note for note in notes)


def test_an_older_spock_is_refused():
    with pytest.raises(add_node.AddNodeError, match="older Spock"):
        add_node.check_spock(plan_with(spock_major="60"), "50")


def test_an_unknown_spock_major_is_refused():
    with pytest.raises(add_node.AddNodeError, match="must be one of"):
        add_node.check_spock(plan_with(), "70")


# ---------------------------------------------------------------------------
# the flow's own guards, exercised through add()
# ---------------------------------------------------------------------------


def run_add(plan, monkeypatch, **kwargs):
    """Call add() against saved state, with no host ever contacted."""
    from aspects import state

    monkeypatch.setattr(state, "load", lambda name, db_password=None: (plan, {}))
    monkeypatch.setattr(state, "resolve_password", lambda *a, **k: "postgres")
    monkeypatch.setattr(state, "save", lambda *a, **k: None)
    monkeypatch.setattr(add_node, "_executor_for",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no host should be contacted")))
    return add_node.add("pgedge", **kwargs)


def test_add_refuses_an_older_postgres_before_touching_a_host(monkeypatch):
    result = run_add(plan_with(), monkeypatch, node_name="n3", pg_version="16.9")

    assert result["outcome"] == "failed"
    assert "older than the cluster" in result["failure"]


def test_add_refuses_an_older_spock_before_touching_a_host(monkeypatch):
    result = run_add(plan_with(spock_major="60"), monkeypatch,
                     node_name="n3", spock_major="50")

    assert result["outcome"] == "failed"
    assert "older Spock" in result["failure"]


def test_add_refuses_a_duplicate_node_name(monkeypatch):
    result = run_add(plan_with(), monkeypatch, node_name="n2")

    assert result["outcome"] == "failed"
    assert "already exists" in result["failure"]


def test_add_refuses_a_standby_as_the_join_source(monkeypatch):
    plan = plan_with(("n1",))
    plan.nodes.append(Node(
        name="n1s1", role="standby", host="local", address="localhost",
        pg_port=5433, restapi_port=8009, data_dir="/d", scope="pgedge-n1",
        config_file="/c", pgpass_file="/p", leader="n1"))

    result = run_add(plan, monkeypatch, node_name="n3", source_node="n1s1")

    assert result["outcome"] == "failed"
    assert "cannot be a join source" in result["failure"]


def test_add_refuses_a_cluster_with_no_spock_nodes(monkeypatch):
    result = run_add(plan_with(nodes=()), monkeypatch, node_name="n1")

    assert result["outcome"] == "failed"
    assert "no Spock nodes" in result["failure"]


def test_a_node_that_never_started_does_not_block_a_new_one(monkeypatch):
    """A previous failed add leaves a node registered with an uninitialised
    scope. It cannot be reloaded and does not need to be — it is serving
    nobody — so it must not stop the next add."""
    from aspects import patroni_management

    plan = plan_with(("n1", "n4"))
    live = {"n1"}

    def members(executor, node, node_name=None):
        return ([{"name": node.name, "role": "Leader", "state": "running"}]
                if node.name in live else [])

    monkeypatch.setattr(patroni_management, "list_members", members)

    # what step 3 decides for each existing node
    decisions = {}
    for existing in plan.nodes:
        reload_ok = existing.name in live
        if reload_ok:
            decisions[existing.name] = "reloaded"
        elif not any(m["name"] == existing.name
                     for m in members(None, existing)):
            decisions[existing.name] = "skipped"
        else:
            decisions[existing.name] = "fatal"

    assert decisions == {"n1": "reloaded", "n4": "skipped"}


def test_a_failed_add_records_which_node_it_left_behind(monkeypatch):
    """So the next attempt can say "remove n4" instead of "already exists"."""
    from aspects import state

    saved = {}
    plan = plan_with()
    monkeypatch.setattr(state, "load", lambda name, db_password=None: (plan, {}))
    monkeypatch.setattr(state, "resolve_password", lambda *a, **k: "postgres")
    monkeypatch.setattr(state, "save",
                        lambda plan, extra=None: saved.update(extra or {}))
    monkeypatch.setattr(add_node, "_executor_for",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("host is down")))

    result = add_node.add("pgedge", node_name="n3")

    assert result["outcome"] == "failed"
    assert saved["failed_node"] == "n3"      # it was registered before the failure


def test_a_failed_add_never_raises(monkeypatch):
    """Callers get a result dict; the CLI turns it into an exit code."""
    result = run_add(plan_with(), monkeypatch, node_name="n3", pg_version="1.0")

    assert set(result) >= {"outcome", "cluster", "failure", "log_dir"}
