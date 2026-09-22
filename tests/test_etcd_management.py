#!/usr/bin/env python3
"""etcd: member selection, the URLs it advertises, and why it failed."""

from aspects import etcd_management
from aspects.cluster_model import Host

from conftest import FakeExecutor, make_hosts


def advertising(name, address, advertise=None):
    host = Host(name=name, address=address)
    host.advertise_address = advertise or address
    return host


def test_three_or_more_hosts_get_a_three_member_cluster(three_hosts):
    members, note = etcd_management.select_members(three_hosts)

    assert [h.name for h in members] == ["a", "b", "c"]
    assert "tolerates one host failure" in note


def test_fewer_hosts_get_one_member_and_say_so(local_host):
    members, note = etcd_management.select_members(local_host)

    assert len(members) == 1
    assert "single point of failure" in note


def test_members_are_deduplicated_by_machine():
    """Two entries for one box cannot both bind 2379/2380."""
    hosts = [Host(name="na", address="localhost"),
             Host(name="nb", address="localhost"),
             Host(name="nc", address="localhost")]

    members, note = etcd_management.select_members(hosts)

    assert len(members) == 1
    assert "single point of failure" in note


def test_urls_come_from_the_advertise_address():
    """initial-cluster and initial-advertise-peer-urls must agree exactly.

    etcd compares them and refuses to start otherwise — the failure that made
    a source-mode deployment die at the etcd step.
    """
    host = advertising("local", "localhost", "172.31.23.166")

    config = etcd_management._yaml_config(host, [host], "pgedge-etcd")

    assert 'initial-advertise-peer-urls: "http://172.31.23.166:2380"' in config
    assert 'initial-cluster: "local=http://172.31.23.166:2380"' in config
    assert "localhost" not in config


def test_env_dialect_uses_the_same_addresses():
    host = advertising("local", "localhost", "172.31.23.166")

    config = etcd_management._env_config(host, [host], "pgedge-etcd")

    assert 'ETCD_INITIAL_ADVERTISE_PEER_URLS="http://172.31.23.166:2380"' in config
    assert 'ETCD_INITIAL_CLUSTER="local=http://172.31.23.166:2380"' in config


def test_real_addresses_are_left_alone():
    hosts = [advertising(*spec) for spec in
             (("a", "10.0.1.11"), ("b", "10.0.1.12"), ("c", "10.0.1.13"))]

    assert etcd_management.endpoints(hosts) == [
        "http://10.0.1.11:2379", "http://10.0.1.12:2379", "http://10.0.1.13:2379",
    ]
    assert etcd_management.initial_cluster(hosts) == (
        "a=http://10.0.1.11:2380,b=http://10.0.1.12:2380,c=http://10.0.1.13:2380"
    )


def test_member_names_are_tokenised():
    assert etcd_management.member_name("node-a.example.com") == "node-a-example-com"
    assert etcd_management.member_name("node a!") == "node-a"
    assert etcd_management.member_name("!!!") == "etcd"


def test_config_dialect_follows_the_family():
    assert etcd_management.config_format_for("rhel") == "yaml"
    assert etcd_management.config_format_for("deb") == "env"
    assert etcd_management.config_path_for("yaml") == "/etc/etcd/etcd.yml"
    assert etcd_management.config_path_for("env") == "/etc/etcd/etcd.conf"


def test_configure_writes_the_unit_dialect_it_is_told_to():
    host = advertising("local", "localhost", "10.0.0.5")
    host.family = "deb"
    executor = FakeExecutor()

    path = etcd_management.configure(executor, host, [host], "pgedge",
                                     config_format="yaml")

    assert path == "/etc/etcd/etcd.yml"
    assert "name:" in executor.written[path]


def test_configure_locks_down_the_data_directory():
    """etcd warns loudly about a world-readable member directory."""
    host = advertising("local", "localhost", "10.0.0.5")
    host.family = "rhel"
    executor = FakeExecutor()

    etcd_management.configure(executor, host, [host], "pgedge")

    assert executor.ran("chmod 700 /var/lib/etcd /var/lib/etcd/local.etcd")


def test_failure_reason_extracts_the_cause_from_etcd_json():
    journal = (
        'Sep 15 09:04 host etcd[1]: {"level":"info","msg":"opened backend db"}\n'
        'Sep 15 09:04 host etcd[1]: {"level":"fatal","caller":"etcdmain/etcd.go:204",'
        '"msg":"discovery failed","error":"--initial-cluster has local='
        'http://localhost:2380 but missing from --initial-advertise-peer-urls",'
        '"stacktrace":"..."}'
    )

    assert etcd_management.failure_reason(journal) == (
        "--initial-cluster has local=http://localhost:2380 but missing from "
        "--initial-advertise-peer-urls"
    )


def test_failure_reason_is_empty_for_a_healthy_journal():
    assert etcd_management.failure_reason("Started etcd.\nActive: running") == ""
    assert etcd_management.failure_reason("") == ""


def test_start_reports_the_reason_not_just_the_exit_code():
    host = advertising("local", "localhost", "10.0.0.5")
    executor = FakeExecutor(replies={
        "list-unit-files": (True, "etcd.service"),
        "systemctl enable --now": (False, "Job failed"),
        "journalctl": (True, '{"level":"fatal","msg":"x","error":"data-dir is locked"}'),
    })

    try:
        etcd_management.start(executor, host, [host])
    except RuntimeError as exc:
        assert "data-dir is locked" in str(exc).splitlines()[0]
    else:
        raise AssertionError("a failed start must raise")
