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


SHIPPED = ["2-node", "3-node", "2-node-ha", "2-node-extensions",
           "3-node-extensions", "3-node-extensions-source", "local-sandbox"]


def test_the_shipped_file_defines_the_shapes_it_documents():
    assert presets.names() == SHIPPED


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_topology_is_valid(name):
    """They are what people copy, so a broken one propagates."""
    assert presets.get(name)


@pytest.mark.parametrize("name", [n for n in SHIPPED if n[0].isdigit()])
def test_each_shipped_topology_has_the_node_count_its_name_claims(name):
    assert presets.get(name)["node_count"] == int(name[0])


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_topology_is_written_in_the_groups(name):
    """The groups are the file's organisation; one shape written flat while
    the others are grouped is the thing that erodes it."""
    raw = presets.load()[name]

    assert set(raw) - {"description"} <= set(presets.GROUP_NAMES), name


@pytest.mark.parametrize("name", SHIPPED)
def test_every_shipped_topology_answers_every_group(name):
    """A missing group is not an error — its defaults apply — but a shipped
    shape is also the worked example of what can be set."""
    raw = presets.load()[name]

    assert set(presets.GROUP_NAMES) <= set(raw), name


def test_a_standby_shape_says_how_its_standby_replicates():
    """Adding a standby and leaving synchronous_mode at off is a real choice,
    but an HA shape that does it by omission is probably an oversight."""
    definition = presets.get("2-node-ha")

    assert definition["standby_of"] == ["n1", "n2"]
    assert definition["synchronous_mode"] == "on"


def test_the_sandbox_shape_runs_on_this_machine_rather_than_the_inventory():
    assert presets.get("local-sandbox")["host_source"] == "localhost"


@pytest.mark.parametrize("name", [n for n in SHIPPED if "extensions" in n or n == "local-sandbox"])
def test_an_extensions_shape_actually_selects_every_add_on(name):
    """The name is the only thing distinguishing it from the plain shape, so
    an add-on missing from the list makes the name a lie."""
    from aspects import pg_extensions

    definition = presets.get(name)
    selected = pg_extensions.parse(definition["pg_extensions"],
                                   default_mode=definition["deploy_mode"])
    assert {entry["name"] for entry in selected} == set(pg_extensions.CATALOG)


def test_the_source_shape_builds_its_add_ons_rather_than_packaging_them():
    """A source-built cluster configures no pgEdge repository, so a packaged
    add-on in it would have nothing to install from."""
    from aspects import pg_extensions

    definition = presets.get("3-node-extensions-source")
    selected = pg_extensions.parse(definition["pg_extensions"],
                                   default_mode=definition["deploy_mode"])
    assert [entry["mode"] for entry in selected] == [pg_extensions.MODE_SOURCE] * 3


def test_a_packaged_shape_does_not_pin_an_add_on_to_a_ref():
    """A ref only means something to a build; parse() refuses the pair, and
    this says so before anyone copies one shape into the other."""
    for name in SHIPPED:
        definition = presets.get(name)
        if definition["deploy_mode"] == "packages":
            assert "@" not in definition["pg_extensions"], name


def test_an_add_on_shape_runs_a_postgres_its_add_ons_support():
    """lolor needs PostgreSQL 16 or newer; a shape pairing it with 15 would
    fail on the first host rather than when it was written."""
    from aspects import pg_extensions

    for name in SHIPPED:
        definition = presets.get(name)
        major = int(str(definition["pg_major"]).split(".")[0])
        for entry in pg_extensions.parse(
                definition["pg_extensions"],
                default_mode=definition["deploy_mode"]):
            floor = pg_extensions.CATALOG[entry["name"]].min_pg_major
            assert major >= floor, f"{name}: {entry['name']} needs {floor}+"


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


# ---------------------------------------------------------------------------
# the groups
# ---------------------------------------------------------------------------


def test_a_grouped_definition_flattens_to_the_keys_everything_else_speaks():
    """Groups are how the file reads; downstream sees one flat dictionary."""
    flat = presets.flatten("demo", {
        "hosts": {"host_source": "localhost"},
        "install": {"deploy_mode": "packages", "pg_major": "18"},
        "cluster": {"node_count": 3},
        "availability": {"standby_of": ["n1"]},
    })

    assert flat == {"host_source": "localhost", "deploy_mode": "packages",
                    "pg_major": "18", "node_count": 3, "standby_of": ["n1"]}


def test_a_definition_written_flat_still_loads():
    """The groups arrived after the file did; a copy from before them is not
    suddenly invalid."""
    assert presets.flatten("demo", minimal(pg_major="17")) == minimal(pg_major="17")


def test_a_key_in_the_wrong_group_names_the_right_one():
    """Accepting it anywhere would make the grouping decorative."""
    with pytest.raises(presets.TopologyError, match="belongs in 'cluster'"):
        presets.flatten("demo", {"install": {"node_count": 2}})


def test_a_group_that_is_not_an_object_says_what_it_should_be():
    with pytest.raises(presets.TopologyError, match="must be an object"):
        presets.flatten("demo", {"install": "packages"})


def test_components_become_the_extension_list():
    flat = presets.flatten("demo", {"components": {
        "lolor": {"install": "packages"},
        "snowflake": {"install": "source", "ref": "v2.0"},
        "ace": {"install": "none"},
    }})

    assert flat["pg_extensions"] == "lolor:packages,snowflake:source@v2.0"


def test_a_component_listed_as_none_is_the_same_as_an_absent_one():
    all_none = presets.flatten("demo", {"components": {
        "lolor": {"install": "none"}, "ace": {"install": "none"}}})

    assert all_none["pg_extensions"] == presets.flatten(
        "demo", {"components": {}})["pg_extensions"] == "none"


def test_a_component_may_be_written_as_the_bare_mode():
    flat = presets.flatten("demo", {"components": {"lolor": "packages"}})

    assert flat["pg_extensions"] == "lolor:packages"


def test_a_ref_on_a_component_that_is_not_installed_is_refused():
    """It would be built by nobody, so it is a leftover from an edit."""
    with pytest.raises(presets.TopologyError, match="built by nobody"):
        presets.flatten("demo", {"components": {
            "lolor": {"install": "none", "ref": "v1.0"}}})


def test_a_misspelled_component_key_is_refused_rather_than_ignored():
    with pytest.raises(presets.TopologyError, match="unknown key"):
        presets.flatten("demo", {"components": {
            "lolor": {"install": "source", "branch": "main"}}})


def test_the_packed_form_is_still_accepted():
    flat = presets.flatten("demo", {"components": {
        "pg_extensions": "pgedge-lolor:source@v1.2.0"}})

    assert flat["pg_extensions"] == "pgedge-lolor:source@v1.2.0"


def test_the_two_component_forms_cannot_be_mixed():
    """They would disagree, and nothing would say which one won."""
    with pytest.raises(presets.TopologyError, match="one form or the other"):
        presets.flatten("demo", {"components": {
            "pg_extensions": "pgedge-lolor",
            "snowflake": {"install": "packages"}}})


def test_an_unusable_component_mode_is_refused_when_the_file_is_read(tmp_path):
    path = write(tmp_path, {"demo": minimal(
        components={"lolor": {"install": "rpm"}})})

    with pytest.raises(presets.TopologyError, match="pg_extensions"):
        presets.get("demo", path)


# ---------------------------------------------------------------------------
# which machines
# ---------------------------------------------------------------------------


def test_an_unknown_host_source_is_refused(tmp_path):
    path = write(tmp_path, {"demo": minimal(hosts={"host_source": "kubernetes"})})

    with pytest.raises(presets.TopologyError, match="host_source"):
        presets.get("demo", path)


def test_localhost_with_an_inventory_file_is_refused(tmp_path):
    """localhost writes an inventory of its own, so the named one would be
    silently ignored."""
    path = write(tmp_path, {"demo": minimal(hosts={
        "host_source": "localhost",
        "inventory_file": "configuration/staging.json"})})

    with pytest.raises(presets.TopologyError, match="would be ignored"):
        presets.get("demo", path)


def test_an_inventory_file_becomes_the_inventory_flag():
    flags = presets.to_flags(presets.flatten("demo", {
        "hosts": {"inventory_file": "configuration/staging.json"}}))

    assert flags == ["--inventory", "configuration/staging.json"]


def test_host_source_is_not_a_flag():
    """The shell acts on it before any flag is built: it decides which
    inventory the rest of them are applied to."""
    assert "--host-source" not in presets.to_flags(
        presets.flatten("demo", {"hosts": {"host_source": "localhost"}}))
