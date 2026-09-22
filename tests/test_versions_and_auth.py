#!/usr/bin/env python3
"""Version arithmetic, psql invocation, and the pg_hba/pgpass the cluster needs."""

import pytest

from aspects import auth_setup, pg_server_management as pgsm

from conftest import FakeExecutor


# ---------------------------------------------------------------------------
# version comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("candidate, minimum, expected", [
    ("17.11", "17.11", True),
    ("18.2", "17.11", True),
    ("17.4", "17.11", False),      # 4 < 11 numerically, not lexically
    ("16.9", "17", False),
    ("17", "17.11", False),
    ("17.11", "17", True),
    ("18beta1", "17.11", True),
    ("18rc1", "18beta2", True),
    ("18.0", "18rc1", True),       # a release beats its own pre-releases
    ("18beta1", "18.0", False),
])
def test_is_at_least(candidate, minimum, expected):
    assert pgsm.is_at_least(candidate, minimum) is expected


def test_version_key_orders_pre_releases_below_the_release():
    ordered = ["17.11", "18beta1", "18beta2", "18rc1", "18.0", "18.1"]

    assert sorted(ordered, key=pgsm.version_key) == ordered


def test_version_key_survives_nonsense():
    assert pgsm.version_key("") == (0, 0, 0, 0)
    assert pgsm.version_key(None) == (0, 0, 0, 0)


def test_major_of():
    assert pgsm.major_of("17.11") == "17"
    assert pgsm.major_of("18beta1") == "18"
    assert pgsm.major_of("") == ""


def test_server_version_reads_the_binary():
    executor = FakeExecutor({"postgres --version": (True, "postgres (PostgreSQL) 17.11")})

    assert pgsm.server_version(executor, "/usr/pgsql-17/bin") == "17.11"


def test_server_version_is_none_when_absent():
    executor = FakeExecutor({"postgres --version": (False, "No such file")})

    assert pgsm.server_version(executor, "/usr/pgsql-18/bin") is None


# ---------------------------------------------------------------------------
# psql
# ---------------------------------------------------------------------------


def test_psql_connects_over_tcp_as_the_database_user():
    executor = FakeExecutor()

    pgsm.psql(executor, "/usr/pgsql-17/bin", 5433, "postgres", "SELECT 1;")

    command = executor.commands[-1]
    assert "/usr/pgsql-17/bin/psql" in command
    assert "-h 127.0.0.1 -p 5433 -U postgres" in command
    assert "ON_ERROR_STOP=1" in command


def test_psql_quotes_the_statement():
    executor = FakeExecutor()

    pgsm.psql(executor, "/bin", 5432, "postgres", "SELECT 'it''s';", check=False)

    assert "SELECT" in executor.commands[-1]


# ---------------------------------------------------------------------------
# pg_hba
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("address, expected", [
    ("10.0.1.11", "10.0.1.11/32"),
    ("fd00::5", "fd00::5/128"),
    ("db-a.example.com", "db-a.example.com"),   # a name takes no mask
    ("localhost", ""),                          # already covered by 127.0.0.1/32
    ("127.0.0.1", ""),
    ("::1", ""),
    ("", ""),
])
def test_hba_target(address, expected):
    assert auth_setup.hba_target(address) == expected


def test_pg_hba_never_masks_a_hostname(plan_factory):
    """`host all all localhost/32` is a syntax error that stops PostgreSQL."""
    plan, _ = plan_factory(node_count=2)

    entries = auth_setup.pg_hba_entries(plan.nodes, "postgres")

    assert not any("localhost/32" in entry for entry in entries)
    assert "host all all 127.0.0.1/32 scram-sha-256" in entries


def test_pg_hba_covers_the_advertise_address(deployed_plan):
    """Replication connects to what the leader advertises, not what was listed."""
    entries = auth_setup.pg_hba_entries(deployed_plan.nodes, "postgres")

    assert "host all all 10.0.1.11/32 scram-sha-256" in entries
    assert "host replication postgres 10.0.1.11/32 scram-sha-256" in entries


def test_pg_hba_adds_extra_cidrs(three_hosts, plan_factory):
    plan, _ = plan_factory(hosts=three_hosts, node_count=3)

    entries = auth_setup.pg_hba_entries(plan.nodes, "postgres",
                                        cidrs=["10.9.0.0/16"])

    assert "host all all 10.9.0.0/16 scram-sha-256" in entries


def test_pgpass_lists_every_node_and_loopback(three_hosts, plan_factory):
    plan, _ = plan_factory(hosts=three_hosts, node_count=3)

    content = auth_setup.pgpass_content(plan.nodes, "postgres", "secret")

    assert content.startswith("# Managed by")
    assert "*:*:*:postgres:secret" in content
    for node in plan.nodes:
        assert f"{node.address}:{node.pg_port}:*:postgres:secret" in content
        assert f"127.0.0.1:{node.pg_port}:*:postgres:secret" in content
    assert len(content.splitlines()) == len(set(content.splitlines()))


def test_pgservice_names_every_node(three_hosts, plan_factory):
    plan, _ = plan_factory(hosts=three_hosts, node_count=3)

    content = auth_setup.pgservice_content(plan.nodes, "postgres", "postgres")

    for node in plan.nodes:
        assert f"[{node.name}]" in content
        assert f"port={node.pg_port}" in content


def test_patroni_pgpass_is_a_wildcard_line():
    executor = FakeExecutor()

    path = auth_setup.write_patroni_pgpass(executor, "/var/lib/pgedge/pgpass_n1",
                                           "postgres", "secret")

    assert executor.written[path] == "*:*:*:postgres:secret\n"
