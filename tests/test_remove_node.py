#!/usr/bin/env python3
"""Shrinking a cluster without taking it down.

Two removals with different hazards. Taking a Spock node out is about what it
leaves behind on the peers: a subscription that outlives the node keeps a
replication slot, and that slot pins WAL on a healthy server forever. Taking a
standby out is about ordering: a scope waiting for a synchronous standby that
has just been stopped blocks every write.

The flow talks to hosts at every step, so these drive it through the executor
cache — pre-populating it with fakes is enough to run the real code paths
without a machine anywhere.
"""

import json

import pytest

from aspects import patroni_management, spock_management
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK, ROLE_STANDBY
from deployment import remove_node
from tests.conftest import FakeExecutor, RecordingLogger


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def plan_with(spock=("n1", "n2", "n3"), standbys=None, spock_major="50",
              **kwargs):
    """A deployed cluster: `spock` multi-master nodes, each on its own host."""
    standbys = standbys or {}
    hosts = [Host(name=f"h{i}", address=f"10.0.1.{10 + i}")
             for i in range(len(spock))]
    for host in hosts:
        host.family = "rhel"
        host.bin_dir = "/usr/pgsql-17/bin"
    plan = ClusterPlan(cluster_name="pgedge", pg_major="17", pg_version="17.11",
                       spock_major=spock_major, deploy_mode="packages",
                       hosts=hosts, **kwargs)
    for index, name in enumerate(spock):
        plan.nodes.append(Node(
            name=name, role=ROLE_SPOCK, host=hosts[index].name,
            address=hosts[index].address, pg_port=5432, restapi_port=8008,
            data_dir=f"/var/lib/pgedge/{name}", scope=f"pgedge-{name}",
            config_file=f"/etc/patroni/{name}.yml",
            pgpass_file=f"/var/lib/pgedge/pgpass_{name}",
            bin_dir="/usr/pgsql-17/bin",
            standbys=list(standbys.get(name, [])),
        ))
    for leader_name, members in standbys.items():
        leader = plan.node(leader_name)
        for offset, name in enumerate(members):
            plan.nodes.append(Node(
                name=name, role=ROLE_STANDBY, host=leader.host,
                address=leader.address, pg_port=5433 + offset,
                restapi_port=8009 + offset,
                data_dir=f"/var/lib/pgedge/{name}", scope=leader.scope,
                config_file=f"/etc/patroni/{name}.yml",
                pgpass_file=f"/var/lib/pgedge/pgpass_{name}",
                bin_dir="/usr/pgsql-17/bin", leader=leader_name,
            ))
    return plan


def members_json(scope, leader, replicas):
    return json.dumps(
        [{"Member": leader, "Host": "10.0.1.10", "Role": "Leader",
          "State": "running", "Cluster": scope}]
        + [{"Member": name, "Host": "10.0.1.10", "Role": "Replica",
            "State": "streaming", "Cluster": scope} for name in replicas]
    )


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Every wait in these flows is a real sleep against a real clock."""
    monkeypatch.setattr(remove_node.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(spock_management.time, "sleep", lambda seconds: None)


def executors_for(plan, replies=None):
    """One fake per host, pre-seeded into the cache `remove()` would fill."""
    shared = dict(replies or {})
    return {host.name: FakeExecutor(replies=dict(shared)) for host in plan.hosts}


# ---------------------------------------------------------------------------
# zodremove: provenance of the removal script
# ---------------------------------------------------------------------------


def test_the_removal_script_comes_from_the_branch_the_extension_was_built_from():
    plan = plan_with()
    node = plan.node("n3")
    node.spock_major = "50"
    executor = FakeExecutor(replies={
        # the source-build checkout is on the same commit as the tag
        "rev-parse --verify -q HEAD": (True, "abc123\n"),
        "v5.0.5^{commit}": (True, "abc123\n"),
    })

    remote, origin = spock_management.stage_zodremove(executor, plan, node,
                                                      "v5.0.5")

    assert remote.endswith("zodremove-v5.0.5.sql")
    assert "checkout" in origin
    assert executor.ran(f"cp /opt/pgedge/build/spock/{spock_management.ZODREMOVE_REPO_PATH}")


def test_a_package_host_fetches_the_removal_script_from_its_branch():
    plan = plan_with()
    node = plan.node("n3")
    executor = FakeExecutor(replies={
        "rev-parse": (False, ""),           # no checkout at all
        "curl": (True, ""),
    })

    remote, origin = spock_management.stage_zodremove(executor, plan, node,
                                                      "v5_STABLE")

    assert origin.endswith("v5_STABLE/samples/Z0DAN/zodremove.sql")
    assert remote.endswith("zodremove-v5_STABLE.sql")


def test_an_air_gapped_spock50_host_falls_back_to_the_bundled_script(logger):
    plan = plan_with()
    node = plan.node("n3")
    executor = FakeExecutor(replies={"rev-parse": (False, ""), "curl": (False, "")})

    remote, origin = spock_management.stage_zodremove(
        executor, plan, node, "v5_STABLE", run_logger=logger
    )

    assert remote.endswith("zodremove-504.sql")
    assert "bundled" in origin
    assert logger.said("falling back to the bundled")


def test_an_air_gapped_spock60_host_says_so_rather_than_loading_a_5_x_script():
    """No spock60 removal script is vendored, and a 5.0.x one reads catalog
    columns that spock60 renamed — it would fail at runtime, not at load."""
    plan = plan_with(spock_major="60")
    node = plan.node("n3")
    executor = FakeExecutor(replies={"rev-parse": (False, ""), "curl": (False, "")})

    with pytest.raises(ValueError, match="no removal script is bundled"):
        spock_management.stage_zodremove(executor, plan, node, "main")


# ---------------------------------------------------------------------------
# zodremove: the call and its verdict
# ---------------------------------------------------------------------------


CLEAN_RUN = """
NOTICE:  NODE REMOVAL SUMMARY
NOTICE:  Node removed: n3
NOTICE:  Total components processed: 6
NOTICE:  Successfully removed: 6
NOTICE:  Errors encountered: 0
"""

PARTIAL_RUN = CLEAN_RUN.replace("Errors encountered: 0", "Errors encountered: 2")


def test_the_removal_runs_on_the_node_being_removed():
    """zodremove compares spock.node_info() with the name it is given and
    refuses if they differ, so the call has to land on the node itself."""
    plan = plan_with()
    node = plan.node("n3")
    executor = FakeExecutor(replies={"CALL spock.remove_node": (True, CLEAN_RUN)})

    ok, _ = spock_management.remove_node(executor, plan, node)

    assert ok is True
    call = next(c for c in executor.commands if "spock.remove_node" in c)
    assert "'n3'" in call
    assert "host=10.0.1.12" in call  # n3's own DSN, not a peer's


def test_a_clean_removal_is_a_verdict_but_errors_are_not():
    plan, node = plan_with(), None
    node = plan.node("n3")

    clean = FakeExecutor(replies={"CALL spock.remove_node": (True, CLEAN_RUN)})
    partial = FakeExecutor(replies={"CALL spock.remove_node": (True, PARTIAL_RUN)})
    broken = FakeExecutor(replies={"CALL spock.remove_node": (False, "ERROR: boom")})
    quiet = FakeExecutor(replies={"CALL spock.remove_node": (True, "CALL")})

    assert spock_management.remove_node(clean, plan, node)[0] is True
    assert spock_management.remove_node(partial, plan, node)[0] is False
    assert spock_management.remove_node(broken, plan, node)[0] is False
    # Finished, but said nothing legible — the peers decide, not this.
    assert spock_management.remove_node(quiet, plan, node)[0] is None


def test_nothing_left_to_remove_is_a_success_not_a_silence():
    """A retried removal processes zero components. That is done, not broken."""
    retried = CLEAN_RUN.replace("processed: 6", "processed: 0") \
                       .replace("removed: 6", "removed: 0")

    assert spock_management.removal_settled(retried) is True
    assert spock_management.removal_settled(PARTIAL_RUN) is False
    assert spock_management.removal_settled("") is False


def test_the_tally_distinguishes_unreadable_from_zero():
    assert spock_management.parse_removal_summary(CLEAN_RUN)["errors"] == 0
    assert spock_management.parse_removal_summary("CALL")["errors"] is None


# ---------------------------------------------------------------------------
# what the peers are left holding
# ---------------------------------------------------------------------------


def test_residue_reports_a_subscription_that_outlived_the_node():
    """The expensive leftover: the subscription keeps a slot, and the slot
    keeps WAL on a node nobody is removing."""
    plan = plan_with()
    peer = plan.node("n1")
    executor = FakeExecutor(replies={
        "FROM spock.node;": (True, "n1,n2,n3"),
        "sub_show_status": (True, "sub_n3_n1|replicating|n3\n"),
        "pg_replication_slots": (True, "spk_db_n3_sub_n3_n1|logical|t|16 MB\n"),
    })

    leftovers = spock_management.residue(lambda n: executor, plan, [peer], "n3")

    assert any("still in spock.node" in line for line in leftovers)
    assert any("sub_n3_n1" in line for line in leftovers)
    assert any("replication slot" in line for line in leftovers)


def test_a_clean_mesh_reports_nothing():
    plan = plan_with()
    peer = plan.node("n1")
    executor = FakeExecutor(replies={
        "FROM spock.node;": (True, "n1,n2"),
        "sub_show_status": (True, "sub_n2_n1|replicating|n2\n"),
        "pg_replication_slots": (True, ""),
    })

    assert spock_management.residue(lambda n: executor, plan, [peer], "n3") == []


# ---------------------------------------------------------------------------
# the Spock-node flow
# ---------------------------------------------------------------------------


def test_the_last_spock_node_is_refused():
    plan = plan_with(spock=("n1",))

    with pytest.raises(remove_node.RemoveNodeError, match="only Spock node"):
        remove_node._remove_spock_node(
            plan, plan.node("n1"), executors_for(plan), RecordingLogger(), [],
            wipe_data=False, drain_timeout=1, force=False, verify_timeout=0,
        )


def test_a_spock_node_is_unwired_with_zodremove_and_then_stopped():
    plan = plan_with()
    executors = executors_for(plan, replies={
        "CALL spock.remove_node": (True, CLEAN_RUN),
        "FROM spock.node;": (True, "n1,n2"),
        "sub_show_status": (True, ""),
        "pg_replication_slots": (True, ""),
        "curl": (True, ""),
    })
    log = RecordingLogger()

    removed = remove_node._remove_spock_node(
        plan, plan.node("n3"), executors, log, [],
        wipe_data=False, drain_timeout=1, force=False, verify_timeout=0,
    )

    assert removed == {"n3"}
    node_host = executors["h2"]
    assert node_host.ran("CALL spock.remove_node")
    assert node_host.ran("patroni-n3")       # stopped
    # The peers were never asked to drop anything by hand.
    assert not executors["h0"].ran("sub_drop")


def test_a_failed_unwiring_changes_nothing_unless_forced():
    plan = plan_with()
    executors = executors_for(plan, replies={
        "CALL spock.remove_node": (False, "ERROR: node n3 has dependent subscriptions"),
        "curl": (True, ""),
    })

    with pytest.raises(remove_node.RemoveNodeError, match="still serving"):
        remove_node._remove_spock_node(
            plan, plan.node("n3"), executors, RecordingLogger(), [],
            wipe_data=False, drain_timeout=1, force=False, verify_timeout=0,
        )

    assert not executors["h2"].ran("stop patroni-n3")


def test_forcing_past_a_failed_unwiring_falls_back_to_dropping_by_hand():
    plan = plan_with()
    executors = executors_for(plan, replies={
        "CALL spock.remove_node": (False, "ERROR: dblink unreachable"),
        "FROM spock.node;": (True, "n1,n2"),
        "sub_show_status": (True, ""),
        "pg_replication_slots": (True, ""),
        "curl": (True, ""),
    })
    warnings = []

    removed = remove_node._remove_spock_node(
        plan, plan.node("n3"), executors, RecordingLogger(), warnings,
        wipe_data=False, drain_timeout=1, force=True, verify_timeout=0,
    )

    assert removed == {"n3"}
    assert any("fell back to a manual detach" in w for w in warnings)
    # Both directions, which is what keeps a slot from surviving the node.
    assert any("sub_drop" in c and "sub_n3_n1" in c
               for c in executors["h0"].commands)
    assert any("sub_drop" in c and "sub_n1_n3" in c
               for c in executors["h2"].commands)


def test_leftovers_on_the_peers_stop_the_removal():
    """A peer still carrying the subscription still carries its slot."""
    plan = plan_with()
    executors = executors_for(plan, replies={
        "CALL spock.remove_node": (True, CLEAN_RUN),
        "FROM spock.node;": (True, "n1,n2,n3"),
        "sub_show_status": (True, "sub_n3_n1|replicating|n3\n"),
        "pg_replication_slots": (True, ""),
        "curl": (True, ""),
    })

    with pytest.raises(remove_node.RemoveNodeError, match="retains WAL"):
        remove_node._remove_spock_node(
            plan, plan.node("n3"), executors, RecordingLogger(), [],
            wipe_data=False, drain_timeout=1, force=False, verify_timeout=0,
        )


def test_a_spock_node_takes_its_own_standbys_with_it():
    plan = plan_with(standbys={"n3": ["n3s1"]})
    executors = executors_for(plan, replies={
        "CALL spock.remove_node": (True, CLEAN_RUN),
        "FROM spock.node;": (True, "n1,n2"),
        "sub_show_status": (True, ""),
        "pg_replication_slots": (True, ""),
        "curl": (True, ""),
    })

    removed = remove_node._remove_spock_node(
        plan, plan.node("n3"), executors, RecordingLogger(), [],
        wipe_data=False, drain_timeout=1, force=False, verify_timeout=0,
    )

    assert removed == {"n3", "n3s1"}
    assert executors["h2"].ran("patroni-n3s1")


# ---------------------------------------------------------------------------
# the standby flow — ordering is the whole point
# ---------------------------------------------------------------------------


def test_removing_the_only_synchronous_standby_relaxes_the_scope_first():
    """A scope told to wait for one synchronous standby, whose only standby is
    then stopped, blocks every write. The requirement has to go first."""
    plan = plan_with(spock=("n1", "n2"), standbys={"n1": ["n1s1"]},
                     synchronous_mode="on", synchronous_node_count=1)
    executors = executors_for(plan, replies={
        "list -f json": (True, members_json("pgedge-n1", "n1", ["n1s1"])),
    })

    removed = remove_node._remove_standby(
        plan, plan.node("n1s1"), executors, RecordingLogger(), [],
        wipe_data=False, force=False,
    )

    assert removed == {"n1s1"}
    leader_commands = executors["h0"].commands
    relaxed = next(i for i, c in enumerate(leader_commands)
                   if "synchronous_mode=off" in c)
    stopped = next(i for i, c in enumerate(leader_commands)
                   if "patroni-n1s1" in c and "stop" in c)
    assert relaxed < stopped, "the scope must stop waiting before the standby goes"


def test_a_scope_with_a_standby_to_spare_keeps_its_synchronous_mode():
    plan = plan_with(spock=("n1", "n2"), standbys={"n1": ["n1s1", "n1s2"]},
                     synchronous_mode="on", synchronous_node_count=1)
    executors = executors_for(plan, replies={
        "list -f json": (True, members_json("pgedge-n1", "n1", ["n1s1", "n1s2"])),
    })
    log = RecordingLogger()

    remove_node._remove_standby(plan, plan.node("n1s2"), executors, log, [],
                                wipe_data=False, force=False)

    assert not executors["h0"].ran("synchronous_mode=off")
    assert any(s.get("message", "").startswith("1 standby(s) remain")
               for s in log.steps)


def test_an_asynchronous_scope_has_nothing_to_relax():
    plan = plan_with(spock=("n1", "n2"), standbys={"n1": ["n1s1"]})
    executors = executors_for(plan, replies={
        "list -f json": (True, members_json("pgedge-n1", "n1", ["n1s1"])),
    })

    remove_node._remove_standby(plan, plan.node("n1s1"), executors,
                                RecordingLogger(), [], wipe_data=False,
                                force=False)

    assert not executors["h0"].ran("edit-config pgedge-n1 --force -s synchronous")


def test_a_promoted_standby_is_refused_because_stopping_it_is_an_outage():
    plan = plan_with(spock=("n1", "n2"), standbys={"n1": ["n1s1"]})
    executors = executors_for(plan, replies={
        # Patroni failed over: the "standby" is the one taking writes now.
        "list -f json": (True, members_json("pgedge-n1", "n1s1", ["n1"])),
    })

    with pytest.raises(remove_node.RemoveNodeError, match="currently taking writes"):
        remove_node._remove_standby(plan, plan.node("n1s1"), executors,
                                    RecordingLogger(), [], wipe_data=False,
                                    force=False)

    assert not executors["h0"].ran("stop patroni-n1s1")


def test_the_leaders_permanent_slot_for_the_standby_is_released():
    """add-standby declares slots.<name>.type=physical so the leader retains
    WAL while the standby is offline. Left behind, it does that forever."""
    plan = plan_with(spock=("n1", "n2"), standbys={"n1": ["n1s1"]})
    executors = executors_for(plan, replies={
        "list -f json": (True, members_json("pgedge-n1", "n1", ["n1s1"])),
    })

    remove_node._remove_standby(plan, plan.node("n1s1"), executors,
                                RecordingLogger(), [], wipe_data=False,
                                force=False)

    assert executors["h0"].ran("-s slots.n1s1=null")


def test_a_standby_whose_slot_cannot_be_dropped_says_how_to_do_it_by_hand():
    plan = plan_with(spock=("n1", "n2"), standbys={"n1": ["n1s1"]})
    executors = executors_for(plan, replies={
        "list -f json": (True, members_json("pgedge-n1", "n1", ["n1s1"])),
        "slots.n1s1=null": (False, "etcd unreachable"),
    })
    warnings = []

    remove_node._remove_standby(plan, plan.node("n1s1"), executors,
                                RecordingLogger(), warnings, wipe_data=False,
                                force=False)

    assert any("patronictl edit-config pgedge-n1 -s slots.n1s1=null" in w
               for w in warnings)


# ---------------------------------------------------------------------------
# the survivors
# ---------------------------------------------------------------------------


def test_the_survivors_are_reloaded_never_restarted():
    """Narrowing access must not interrupt a node that is serving traffic."""
    plan = plan_with(spock=("n1", "n2"))
    executors = executors_for(plan)

    remove_node._narrow_survivors(plan, executors, RecordingLogger(), [])

    for name in ("h0", "h1"):
        assert executors[name].ran("reload pgedge-")
        assert not executors[name].ran("restart pgedge-")


def test_the_branch_for_the_removal_script_follows_the_cluster_build():
    plan = plan_with(source_build={"spock_branch": "v5.0.5"})

    assert remove_node.spock_branch_for(plan, plan.node("n3")) == "v5.0.5"


def test_a_node_on_the_other_spock_major_uses_that_majors_branch():
    """Its extension was not built from the cluster's branch, so its removal
    script must not come from there either."""
    plan = plan_with(source_build={"spock_branch": "v5.0.5"})
    node = plan.node("n3")
    node.spock_major = "60"

    assert remove_node.spock_branch_for(plan, node) != "v5.0.5"
