#!/usr/bin/env python3
"""Spock: which zodan script a node gets, and how the join is judged."""

import pytest

from aspects import pg_server_management, spock_management as sm
from aspects.cluster_model import Node

from conftest import FakeExecutor, RecordingLogger


@pytest.fixture(autouse=True)
def no_real_psql(monkeypatch):
    """psql_file is exercised elsewhere; here it only has to not run."""
    monkeypatch.setattr(pg_server_management, "psql_file",
                        lambda *args, **kwargs: (0, ""))


def a_node(name="n3", spock_major=""):
    node = Node(name=name, role="spock", host="local", address="localhost",
                pg_port=5434, restapi_port=8010, data_dir="/d",
                scope="pgedge-n3", config_file="/c", pgpass_file="/p",
                bin_dir="/usr/pgsql-17/bin")
    node.spock_major = spock_major
    return node


# ---------------------------------------------------------------------------
# which script
# ---------------------------------------------------------------------------


def test_bundled_script_is_chosen_by_major():
    assert sm.zodan_script("50").name == "zodan-511.sql"
    assert sm.zodan_script("60").name == "zodan-600.sql"


def test_unknown_major_is_rejected():
    with pytest.raises(ValueError, match="No zodan script"):
        sm.zodan_script("70")


def test_zodan_comes_from_the_source_checkout_when_it_is_on_that_branch(deployed_plan):
    deployed_plan.deploy_mode = "source"
    node = a_node(spock_major="60")
    executor = FakeExecutor({"rev-parse --abbrev-ref": (True, "main")})

    remote, origin = sm.stage_zodan(executor, deployed_plan, node, "main")

    assert remote.endswith("zodan-main.sql")
    assert "checkout" in origin
    assert executor.ran("samples/Z0DAN/zodan.sql")


def test_zodan_is_fetched_from_the_branch_when_there_is_no_checkout(deployed_plan):
    node = a_node(spock_major="60")
    executor = FakeExecutor({"rev-parse --abbrev-ref": (False, "")})

    _, origin = sm.stage_zodan(executor, deployed_plan, node, "main")

    assert origin == ("https://raw.githubusercontent.com/pgEdge/spock/main/"
                      "samples/Z0DAN/zodan.sql")


def test_zodan_falls_back_to_the_bundled_copy_offline(deployed_plan):
    node = a_node(spock_major="60")
    executor = FakeExecutor({"rev-parse --abbrev-ref": (False, ""),
                             "curl": (False, "")})
    logger = RecordingLogger()

    remote, origin = sm.stage_zodan(executor, deployed_plan, node, "main",
                                    run_logger=logger)

    assert remote.endswith("zodan-600.sql")
    assert "bundled" in origin
    assert logger.said("falling back")


def test_a_spock60_node_loads_zodan_from_main(deployed_plan):
    """The mixed-version add depends on this: v5_STABLE's script forbids it."""
    deployed_plan.spock_major = "50"
    node = a_node(spock_major="60")
    executor = FakeExecutor({"rev-parse --abbrev-ref": (False, "")})

    sm.load_zodan(executor, deployed_plan, node)

    assert executor.ran("/pgEdge/spock/main/samples/Z0DAN/zodan.sql")


def test_a_spock50_node_loads_zodan_from_v5_stable(deployed_plan):
    deployed_plan.spock_major = "50"
    node = a_node(spock_major="50")
    executor = FakeExecutor({"rev-parse --abbrev-ref": (False, "")})

    sm.load_zodan(executor, deployed_plan, node)

    assert executor.ran("/pgEdge/spock/v5_STABLE/samples/Z0DAN/zodan.sql")


def test_a_stale_pin_for_another_major_is_ignored(deployed_plan):
    """Older versions of this tool auto-saved zodan_sql into cluster state."""
    deployed_plan.spock_major = "60"
    deployed_plan.zodan_sql = "zodan-511.sql"
    node = a_node(spock_major="60")
    executor = FakeExecutor({"rev-parse --abbrev-ref": (False, "")})
    logger = RecordingLogger()

    _, origin = sm.stage_zodan(executor, deployed_plan, node, "main",
                               run_logger=logger)

    assert "raw.githubusercontent.com" in origin
    assert logger.said("ignoring the pinned zodan-511.sql")


def test_a_pin_matching_the_major_is_honoured(deployed_plan):
    deployed_plan.spock_major = "50"
    deployed_plan.zodan_sql = "zodan-511.sql"
    node = a_node(spock_major="50")

    _, origin = sm.stage_zodan(FakeExecutor(), deployed_plan, node, "v5_STABLE")

    assert "bundled zodan-511.sql" in origin


def test_a_pin_is_ignored_for_a_node_on_the_other_major(deployed_plan):
    deployed_plan.spock_major = "50"
    deployed_plan.zodan_sql = "zodan-511.sql"
    node = a_node(spock_major="60")
    executor = FakeExecutor({"rev-parse --abbrev-ref": (False, "")})

    _, origin = sm.stage_zodan(executor, deployed_plan, node, "main")

    assert "raw.githubusercontent.com" in origin


def test_staged_script_is_readable_by_the_database_user(deployed_plan):
    node = a_node(spock_major="60")
    executor = FakeExecutor({"rev-parse --abbrev-ref": (False, "")})

    remote, _ = sm.stage_zodan(executor, deployed_plan, node, "main")

    assert executor.ran(f"chown postgres: {remote}")
    assert executor.ran(f"chmod 644 {remote}")


# ---------------------------------------------------------------------------
# extensions and the join
# ---------------------------------------------------------------------------


def test_create_extensions_installs_spock_and_dblink(deployed_plan):
    executor = FakeExecutor()

    created = sm.create_extensions(executor, deployed_plan, a_node())

    assert created == list(sm.REQUIRED_EXTENSIONS)
    assert executor.ran("CREATE EXTENSION IF NOT EXISTS spock;")
    assert executor.ran("CREATE EXTENSION IF NOT EXISTS dblink;")


def test_add_node_calls_spock_add_node_on_the_new_node(deployed_plan):
    source, new = deployed_plan.node("n1"), deployed_plan.node("n2")
    executor = FakeExecutor({"CALL spock.add_node": (True, "Success rate: %100")})

    ok, output = sm.add_node(executor, deployed_plan, source, new)

    assert ok is True
    call = [c for c in executor.commands if "CALL spock.add_node" in c][-1]
    assert "'n1'" in call and "'n2'" in call
    assert f"port={new.pg_port}" in call


def test_add_node_reports_a_partial_join_as_failure(deployed_plan):
    source, new = deployed_plan.node("n1"), deployed_plan.node("n2")
    executor = FakeExecutor({"CALL spock.add_node": (True, "Success rate: %60")})

    ok, output = sm.add_node(executor, deployed_plan, source, new)

    assert ok is False
    assert sm.parse_success_rate(output) == 60


def test_parse_success_rate():
    assert sm.parse_success_rate("... Success rate: %100 ...") == 100
    assert sm.parse_success_rate("Success rate: 75") == 75
    assert sm.parse_success_rate("no verdict here") is None
    assert sm.parse_success_rate(None) is None


def test_enable_ddl_replication_sets_each_guc_separately(deployed_plan):
    """ALTER SYSTEM is rejected inside a multi-statement batch."""
    executor = FakeExecutor()

    sm.enable_ddl_replication(executor, deployed_plan, a_node())

    for guc in sm.DDL_REPLICATION_GUCS:
        assert executor.count(guc) >= 1


def test_sub_name_follows_zodans_convention():
    assert sm.sub_name("n1", "n3") == "sub_n1_n3"
