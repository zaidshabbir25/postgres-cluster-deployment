#!/usr/bin/env python3
"""The inventory that names machines, and the state that remembers a cluster."""

import json

import pytest

from aspects import inventory, state
from aspects.cluster_model import ClusterPlan, Host


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


def test_enabled_hosts_are_loaded_with_their_defaults(inventory_file):
    path = inventory_file(
        [
            {"name": "a", "host": "10.0.1.11", "username": "rocky", "enabled": True},
            {"name": "b", "host": "10.0.1.12", "enabled": True},
            {"name": "parked", "host": "10.0.1.13", "enabled": False},
        ],
        defaults={"cluster_name": "demo", "pg_major": "17"},
    )

    hosts, defaults = inventory.load(path)

    assert [h.name for h in hosts] == ["a", "b"]
    assert hosts[0].username == "rocky"
    assert hosts[1].username == "root"       # the default
    assert hosts[1].port == 22
    assert defaults["cluster_name"] == "demo"


def test_a_missing_name_falls_back_to_the_address(inventory_file):
    path = inventory_file([{"host": "10.0.1.11", "enabled": True}])

    hosts, _ = inventory.load(path)

    assert hosts[0].name == "10.0.1.11"


def test_the_local_flag_is_preserved(inventory_file):
    path = inventory_file([{"name": "local", "host": "localhost", "local": True}])

    hosts, _ = inventory.load(path)

    assert hosts[0].local is True


def test_a_host_without_an_address_is_an_error(inventory_file):
    path = inventory_file([{"name": "nameless"}])

    with pytest.raises(inventory.InventoryError, match='"host" is required'):
        inventory.load(path)


def test_duplicate_names_are_an_error(inventory_file):
    path = inventory_file([
        {"name": "a", "host": "10.0.1.11"},
        {"name": "a", "host": "10.0.1.12"},
    ])

    with pytest.raises(inventory.InventoryError, match="duplicate host name"):
        inventory.load(path)


def test_an_inventory_with_no_enabled_hosts_is_an_error(inventory_file):
    path = inventory_file([{"name": "a", "host": "10.0.1.11", "enabled": False}])

    with pytest.raises(inventory.InventoryError, match="no enabled hosts"):
        inventory.load(path)


def test_broken_json_says_so(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(inventory.InventoryError, match="not valid JSON"):
        inventory.load(path)


def test_a_missing_hosts_array_says_so(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"defaults": {}}), encoding="utf-8")

    with pytest.raises(inventory.InventoryError, match="missing top-level"):
        inventory.load(path)


def test_a_missing_file_says_where_it_looked(tmp_path):
    with pytest.raises(inventory.InventoryError, match="not found"):
        inventory.load(tmp_path / "absent.json")


def test_a_world_readable_key_is_reported(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("-----BEGIN-----", encoding="utf-8")
    key.chmod(0o644)
    host = Host(name="a", address="10.0.1.11", key_file=str(key))

    warnings = inventory.check_key_permissions([host])

    assert warnings and "chmod 600" in warnings[0]


def test_a_locked_down_key_is_not_reported(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("-----BEGIN-----", encoding="utf-8")
    key.chmod(0o600)

    assert inventory.check_key_permissions(
        [Host(name="a", address="10.0.1.11", key_file=str(key))]) == []


def test_the_example_inventory_is_valid():
    """It is what the docs tell people to copy."""
    hosts, defaults = inventory.load(inventory.EXAMPLE_INVENTORY)

    assert hosts
    assert defaults["base_port"] == 5432


# ---------------------------------------------------------------------------
# saved state
# ---------------------------------------------------------------------------


def test_a_plan_survives_a_round_trip(state_dir, deployed_plan):
    state.save(deployed_plan, extra={"outcome": "succeeded"})

    loaded, metadata = state.load("pgedge")

    assert metadata["outcome"] == "succeeded"
    assert [n.name for n in loaded.nodes] == [n.name for n in deployed_plan.nodes]
    assert loaded.node("n1").pg_port == deployed_plan.node("n1").pg_port
    assert loaded.node("n1").scope == deployed_plan.node("n1").scope


def test_the_advertise_address_and_spock_major_survive(state_dir, deployed_plan):
    """Both were added later; a state file that drops them breaks add-node."""
    deployed_plan.node("n2").spock_major = "60"
    state.save(deployed_plan)

    loaded, _ = state.load("pgedge")

    assert loaded.node("n1").advertise_address == "10.0.1.11"
    assert loaded.host("local").advertise_address == "10.0.1.11"
    assert loaded.node("n2").spock_major == "60"


def test_the_password_is_not_written_to_disk(state_dir, deployed_plan):
    deployed_plan.db_password = "secret"

    path = state.save(deployed_plan)

    assert "secret" not in path.read_text(encoding="utf-8")


def test_key_files_are_not_written_to_disk(state_dir, deployed_plan):
    deployed_plan.hosts[0].key_file = "/root/.ssh/id_rsa"

    path = state.save(deployed_plan)

    assert "id_rsa" not in path.read_text(encoding="utf-8")


def test_listing_and_deleting(state_dir, deployed_plan):
    state.save(deployed_plan)

    assert state.list_clusters() == ["pgedge"]
    assert state.latest_cluster() == "pgedge"

    state.delete("pgedge")

    assert state.list_clusters() == []


def test_state_from_a_newer_build_still_loads(state_dir, deployed_plan):
    """A state file outlives the code that wrote it.

    Deploy on one branch, operate from another that predates a feature, and the
    file carries settings this build has never heard of. Refusing to load it
    would turn a missing feature into an unusable cluster.
    """
    import json

    path = state.save(deployed_plan)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["plan"]["log_retention_days"] = 7
    payload["plan"]["nodes"][0]["future_node_field"] = "x"
    payload["plan"]["hosts"][0]["future_host_field"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded, _ = state.load("pgedge")

    assert [n.name for n in loaded.nodes] == [n.name for n in deployed_plan.nodes]
    assert loaded.node("n1").pg_port == deployed_plan.node("n1").pg_port


def test_settings_this_build_does_not_know_survive_a_save(state_dir, deployed_plan):
    """Otherwise operating from an older branch silently deletes them."""
    import json

    path = state.save(deployed_plan)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["plan"]["log_retention_days"] = 7
    payload["plan"]["nodes"][0]["future_node_field"] = "x"
    payload["plan"]["hosts"][0]["future_host_field"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded, _ = state.load("pgedge")
    state.save(loaded)                       # what add-node does at the end

    after = json.loads(path.read_text(encoding="utf-8"))["plan"]
    assert after["log_retention_days"] == 7
    assert after["nodes"][0]["future_node_field"] == "x"
    assert after["hosts"][0]["future_host_field"] is True


def test_loading_an_unknown_cluster_says_what_exists(state_dir):
    with pytest.raises(FileNotFoundError, match="No saved state"):
        state.load("nope")


# ---------------------------------------------------------------------------
# the plan itself
# ---------------------------------------------------------------------------


def test_plan_helpers(deployed_plan):
    assert [n.name for n in deployed_plan.spock_nodes] == ["n1", "n2"]
    assert deployed_plan.standby_nodes == []
    assert [n.name for n in deployed_plan.nodes_on("local")] == ["n1", "n2"]
    assert set(deployed_plan.scopes()) == {"pgedge-n1", "pgedge-n2"}


def test_dsn_carries_the_password_for_dblink(deployed_plan):
    dsn = deployed_plan.node("n1").dsn("postgres", "postgres", "secret")

    assert "host=localhost" in dsn
    assert "port=5432" in dsn
    assert "password=secret" in dsn


def test_summary_lines_describe_the_cluster(deployed_plan):
    text = "\n".join(deployed_plan.summary_lines())

    assert "pgedge" in text
    assert "n1" in text and "n2" in text
