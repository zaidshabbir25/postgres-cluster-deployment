#!/usr/bin/env python3
"""The Patroni callback that retargets Spock peers when a standby is promoted.

The script is shell, not Python, because it has to run on a cluster host with
no repository checkout. So these tests *run* it, against a stub psql, rather
than asserting on the text it is made of — a rendered script that parses but
calls the wrong thing would pass the second kind of test and fail in the only
situation it exists for.
"""

import os
import subprocess

import pytest

from aspects import failover_callback as callback
from aspects.cluster_model import Host


@pytest.fixture
def cluster(plan_factory):
    plan, _ = plan_factory(
        hosts=[Host(name="local", address="localhost", local=True)],
        node_count=3, standby_of=["n1"], auto_repair_failover=True)
    plan.db_password = "secret"
    for node in plan.nodes:
        node.bin_dir = "/usr/pgsql-17/bin"
    return plan


@pytest.fixture
def staged(tmp_path, cluster):
    """The rendered script, with psql replaced by a recorder.

    Returns a runner taking the role Patroni would pass, plus whatever the
    stub should print for the subscription query.
    """
    calls = tmp_path / "psql-calls.txt"
    log = tmp_path / "role-change.log"

    def build(peers="n2|host=10.0.0.2 port=5432 dbname=postgres user=postgres",
              fail_with=""):
        psql = tmp_path / "psql"
        psql.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "$*" >> {calls}\n'
            'for arg in "$@"; do\n'
            '  case "$arg" in\n'
            f'    *sub_show_status*) printf "%s\\n" {peers!r}; exit 0 ;;\n'
            + (f'    *node_add_interface*) echo {fail_with!r}; exit 1 ;;\n'
               if fail_with else "")
            + '  esac\n'
            'done\n'
            'exit 0\n',
            encoding="utf-8")
        psql.chmod(0o755)

        node = cluster.node("n1s1")
        node.bin_dir = str(tmp_path)
        script = tmp_path / "on-role-change.sh"
        script.write_text(
            callback.render(cluster, node,
                            spock_name=callback.spock_name_for(cluster, node)),
            encoding="utf-8")
        script.chmod(0o755)

        def run(role):
            env = dict(os.environ, PGEDGE_ROLE_CHANGE_LOG=str(log))
            subprocess.run([str(script), "on_role_change", role, node.scope],
                           env=env, check=True, timeout=30)
            return (calls.read_text(encoding="utf-8") if calls.exists() else "",
                    log.read_text(encoding="utf-8"))

        return run

    return build


# ---------------------------------------------------------------------------
# what it does when it fires
# ---------------------------------------------------------------------------


def test_a_promotion_points_every_peer_at_this_instance(staged):
    commands, _ = staged()("primary")

    assert "sub_disable" in commands
    assert "node_add_interface" in commands
    assert "sub_alter_interface" in commands
    assert "sub_enable" in commands


def test_it_repairs_the_subscription_named_for_the_spock_node(staged):
    """A standby serves its leader's Spock identity. Using its own name would
    name a subscription that does not exist."""
    commands, _ = staged()("primary")

    assert "sub_n1_n2" in commands
    assert "sub_n1s1_n2" not in commands


def test_the_interface_carries_this_instance_port(staged):
    """The whole point: the peers are pointed at where the node answers now,
    which is the promoted member's port, not the old leader's."""
    commands, _ = staged()("primary")

    assert "n1_promoted_5435" in commands


def test_the_subscription_is_enabled_immediately(staged):
    """Without immediate := true the peer keeps reporting 'down' until its
    apply worker next cycles, which looks like the repair did nothing."""
    commands, _ = staged()("primary")

    assert "sub_enable" in commands and "true" in commands


def test_patroni_also_passes_the_older_spelling_of_the_role(staged):
    """Patroni called it 'master' before it called it 'primary'."""
    commands, _ = staged()("master")

    assert "sub_alter_interface" in commands


# ---------------------------------------------------------------------------
# when it should do nothing
# ---------------------------------------------------------------------------


def test_a_demotion_touches_nothing(staged):
    """on_role_change also fires on the node that became a replica."""
    commands, log = staged()("replica")

    assert "sub_alter_interface" not in commands
    assert "nothing to do" in log


def test_a_node_with_no_subscriptions_yet_is_not_an_error(staged):
    """It fires during the first bootstrap too, before the mesh is wired."""
    commands, log = staged(peers="")("primary")

    assert "sub_alter_interface" not in commands
    assert "nothing to retarget" in log


def test_an_interface_that_already_exists_is_tolerated(staged):
    """A scope that has failed over to this member before already has it, and
    the switch that follows is what actually matters."""
    _, log = staged(
        fail_with="ERROR:  interface n1_promoted_5435 already exists",
    )("primary")

    assert "already present" in log
    assert "add interface failed" not in log


def test_every_run_is_logged_with_what_it_was_called_with(staged):
    _, log = staged()("primary")

    assert "role=primary" in log
    assert "done" in log


# ---------------------------------------------------------------------------
# how it reaches the configuration
# ---------------------------------------------------------------------------


def test_patroni_is_told_about_the_callback_only_when_it_is_enabled(cluster):
    from aspects import patroni_management

    node = cluster.node("n1")
    enabled = patroni_management.build_config(cluster, node)

    cluster.auto_repair_failover = False
    disabled = patroni_management.build_config(cluster, node)

    assert enabled["postgresql"]["callbacks"]["on_role_change"] == \
        callback.script_path("n1")
    assert "callbacks" not in disabled["postgresql"]


def test_the_standby_gets_a_callback_too(cluster):
    """It is the member that will be promoted, so it is the one that has to
    carry the script."""
    from aspects import patroni_management

    config = patroni_management.build_config(cluster, cluster.node("n1s1"))

    assert config["postgresql"]["callbacks"]["on_role_change"] == \
        callback.script_path("n1s1")


def test_a_spock_node_serves_its_own_identity(cluster):
    assert callback.spock_name_for(cluster, cluster.node("n2")) == "n2"
