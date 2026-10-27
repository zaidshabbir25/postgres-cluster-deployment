#!/usr/bin/env python3
"""Named cluster shapes, read from configuration/topologies.json.

The point of a named shape is that it was agreed once, so the thing worth
testing hardest is that a bad one is refused when it is *read* rather than
twenty minutes into a source build. The shipped topologies are checked too:
they are what people copy.
"""

import json

import pytest

from aspects import topology_presets as presets


def write(tmp_path, topologies):
    path = tmp_path / "topologies.json"
    path.write_text(json.dumps({"topologies": topologies}), encoding="utf-8")
    return path


def minimal(**overrides):
    definition = {"node_count": 2, "deploy_mode": "packages"}
    definition.update(overrides)
    return definition


# ---------------------------------------------------------------------------
# the file that ships
# ---------------------------------------------------------------------------


def test_the_shipped_file_defines_a_two_and_a_three_node_shape():
    assert presets.names() == ["2-node", "3-node"]


@pytest.mark.parametrize("name", ["2-node", "3-node"])
def test_every_shipped_topology_is_valid(name):
    """They are what people copy, so a broken one propagates."""
    assert presets.get(name)


@pytest.mark.parametrize("name,count", [("2-node", 2), ("3-node", 3)])
def test_each_shipped_topology_has_the_node_count_its_name_claims(name, count):
    assert presets.get(name)["node_count"] == count


def test_every_shipped_topology_says_what_it_is_for():
    """A name alone does not say why you would pick it."""
    for name in presets.names():
        assert presets.get(name).get("description", "").strip(), name


def test_the_shipped_keys_are_the_vocabulary_used_elsewhere():
    """Key names match inventory.json's defaults and ClusterPlan's fields, so
    there is one spelling of node_count across all three."""
    from aspects import inventory

    _, defaults = inventory.load(inventory.EXAMPLE_INVENTORY)
    shared = set(presets.get("2-node")) & set(defaults)

    assert {"node_count", "cluster_name", "pg_major", "db_name"} <= shared


# ---------------------------------------------------------------------------
# what it becomes
# ---------------------------------------------------------------------------


def test_a_topology_turns_into_the_flags_it_is_equivalent_to():
    flags = presets.to_flags(presets.get("3-node"))

    assert "--nodes" in flags and flags[flags.index("--nodes") + 1] == "3"
    assert "--mode" in flags and flags[flags.index("--mode") + 1] == "packages"


def test_an_empty_value_is_left_out_rather_than_passed_as_empty():
    """--pg-version '' would override the inventory default with nothing."""
    flags = presets.to_flags(minimal(pg_version="", cluster_name="demo"))

    assert "--pg-version" not in flags
    assert "--cluster" in flags


def test_standbys_become_one_flag_each():
    flags = presets.to_flags(minimal(node_count=3, standby_of=["n1", "n2"]))

    assert flags.count("--standby") == 2
    assert "n1" in flags and "n2" in flags


def test_a_switch_is_present_or_absent_not_true_or_false():
    assert "--sync-strict" in presets.to_flags(
        minimal(synchronous_mode_strict=True))
    assert "--sync-strict" not in presets.to_flags(
        minimal(synchronous_mode_strict=False))


def test_a_description_is_not_a_flag():
    flags = presets.to_flags(minimal(description="a shape"))

    assert not any("description" in flag for flag in flags)


# ---------------------------------------------------------------------------
# what it refuses, and when
# ---------------------------------------------------------------------------


def test_an_unknown_name_lists_the_ones_that_exist():
    with pytest.raises(presets.TopologyError, match="2-node, 3-node"):
        presets.get("17-node")


def test_a_source_build_without_an_exact_version_is_refused(tmp_path):
    """It has to fetch one tarball, and finding that out after the hosts are
    prepared wastes the preparation."""
    path = write(tmp_path, {"s": minimal(deploy_mode="source")})

    with pytest.raises(presets.TopologyError, match="exact pg_version"):
        presets.get("s", path)


def test_a_branch_on_a_packaged_shape_is_refused(tmp_path):
    path = write(tmp_path, {"s": minimal(spock_branch="main")})

    with pytest.raises(presets.TopologyError, match="applies to a source build"):
        presets.get("s", path)


def test_a_standby_for_a_node_that_will_never_exist_is_refused(tmp_path):
    """Nodes are generated n1..nN, so n4 in a two-node shape is a typo."""
    path = write(tmp_path, {"s": minimal(standby_of=["n1", "n4"])})

    with pytest.raises(presets.TopologyError, match="n4"):
        presets.get("s", path)


def test_a_misspelled_key_is_refused_rather_than_ignored(tmp_path):
    """Silently dropping `nodes` would deploy two nodes when three were
    written down, which is the failure a named shape exists to prevent."""
    path = write(tmp_path, {"s": {"nodes": 3}})

    with pytest.raises(presets.TopologyError, match="unknown key"):
        presets.get("s", path)


@pytest.mark.parametrize("bad", [
    {"deploy_mode": "rpm"},
    {"repo_channel": "nightly"},
    {"spock_major": "70"},
    {"synchronous_mode": "maybe"},
    {"node_count": 0},
    {"node_count": "two"},
])
def test_a_value_outside_its_range_is_refused(tmp_path, bad):
    path = write(tmp_path, {"s": minimal(**bad)})

    with pytest.raises(presets.TopologyError):
        presets.get("s", path)


def test_an_unusable_extension_list_is_refused(tmp_path):
    path = write(tmp_path, {"s": minimal(pg_extensions="pgedge-timescale")})

    with pytest.raises(presets.TopologyError, match="unknown extension"):
        presets.get("s", path)


def test_a_valid_extension_list_passes(tmp_path):
    path = write(tmp_path, {"s": minimal(pg_extensions="pgedge-lolor,pgedge-ace")})

    assert presets.get("s", path)


# ---------------------------------------------------------------------------
# a broken file
# ---------------------------------------------------------------------------


def test_a_missing_file_says_where_it_looked(tmp_path):
    with pytest.raises(presets.TopologyError, match="no topologies file"):
        presets.load(tmp_path / "absent.json")


def test_broken_json_says_so(tmp_path):
    path = tmp_path / "topologies.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(presets.TopologyError, match="not valid JSON"):
        presets.load(path)


def test_a_file_with_no_topologies_object_says_what_it_should_hold(tmp_path):
    path = tmp_path / "topologies.json"
    path.write_text(json.dumps({"defaults": {}}), encoding="utf-8")

    with pytest.raises(presets.TopologyError, match="no 'topologies' object"):
        presets.load(path)
