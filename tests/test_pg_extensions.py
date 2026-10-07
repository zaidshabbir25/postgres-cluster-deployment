#!/usr/bin/env python3
"""Optional extensions: lolor and snowflake.

Two things here are worth more than the plumbing. The first is the package
name, which the two families spell differently and neither spelling is
derivable from the other. The second is the node identity GUC: both extensions
generate ids that embed a node number, nothing detects two nodes sharing one,
and the collision only ever surfaces as duplicate keys long afterwards.
"""

import pytest

from aspects import pg_extensions
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK, ROLE_STANDBY
from tests.conftest import FakeExecutor, RecordingLogger


def node(name, role=ROLE_SPOCK, leader=None):
    return Node(name=name, role=role, host="h0", address="10.0.1.10",
                pg_port=5432, restapi_port=8008, data_dir=f"/var/lib/pgedge/{name}",
                scope=f"pgedge-{name}", config_file=f"/etc/patroni/{name}.yml",
                pgpass_file="/p", bin_dir="/usr/pgsql-17/bin", leader=leader)


def plan_with(names=("n1", "n2"), standbys=(), **kwargs):
    host = Host(name="h0", address="10.0.1.10")
    host.family = kwargs.pop("family", "rhel")
    host.bin_dir = "/usr/pgsql-17/bin"
    plan = ClusterPlan(cluster_name="pgedge", pg_major="17", pg_version="17.11",
                       hosts=[host], **kwargs)
    plan.nodes = [node(name) for name in names]
    plan.nodes += [node(name, ROLE_STANDBY, leader=leader)
                   for name, leader in standbys]
    return plan


# ---------------------------------------------------------------------------
# reading --pg-extensions
# ---------------------------------------------------------------------------


def test_the_default_is_none_and_costs_nothing():
    assert pg_extensions.parse("none") == []
    assert pg_extensions.parse("") == []
    assert pg_extensions.parse(None) == []


def test_both_extensions_can_be_named_with_or_without_the_package_prefix():
    one = pg_extensions.parse("pgedge-lolor,pgedge-snowflake")
    two = pg_extensions.parse("lolor, snowflake")

    assert [item["name"] for item in one] == ["lolor", "snowflake"]
    assert one == two


def test_the_mode_follows_the_cluster_unless_an_entry_overrides_it():
    """A source-built cluster has no pgEdge repository to install from."""
    assert pg_extensions.parse("pgedge-lolor", default_mode="source") == [
        {"name": "lolor", "mode": "source", "ref": "main"}
    ]
    assert pg_extensions.parse("pgedge-lolor:packages", default_mode="source") == [
        {"name": "lolor", "mode": "packages", "ref": ""}
    ]


def test_a_source_entry_can_name_the_branch_or_tag_to_build():
    assert pg_extensions.parse("pgedge-lolor:source@v1.2.0") == [
        {"name": "lolor", "mode": "source", "ref": "v1.2.0"}
    ]


def test_a_ref_on_a_packaged_entry_is_refused_rather_than_ignored():
    """A package comes at whatever version the channel carries, so a ref there
    would silently mean nothing."""
    with pytest.raises(ValueError, match="only applies to a source build"):
        pg_extensions.parse("pgedge-lolor:packages@v1.2.0")


def test_an_unknown_extension_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="pgedge-lolor, pgedge-snowflake"):
        pg_extensions.parse("pgedge-timescale")


def test_naming_the_same_extension_twice_is_refused():
    with pytest.raises(ValueError, match="listed twice"):
        pg_extensions.parse("pgedge-lolor:source,lolor:packages")


def test_the_selection_round_trips_through_the_flag_value():
    text = "pgedge-lolor:source@v1.2.0,pgedge-snowflake:packages"

    assert pg_extensions.unparse(pg_extensions.parse(text)) == text
    assert pg_extensions.unparse([]) == "none"


# ---------------------------------------------------------------------------
# package names
# ---------------------------------------------------------------------------


def test_each_family_spells_the_package_its_own_way():
    lolor = pg_extensions.CATALOG["lolor"]
    snowflake = pg_extensions.CATALOG["snowflake"]

    assert lolor.package("rhel", "18") == "pgedge-lolor_18"
    assert lolor.package("deb", "17") == "pgedge-postgresql-17-lolor"
    assert snowflake.package("rhel", "17") == "pgedge-snowflake_17"
    assert snowflake.package("deb", "17") == "pgedge-postgresql-17-snowflake"


def test_only_the_packaged_entries_become_packages():
    selections = pg_extensions.parse("pgedge-lolor:packages,pgedge-snowflake:source")

    assert pg_extensions.packages_for(selections, "rhel", "17") == \
        ["pgedge-lolor_17"]


def test_installing_nothing_runs_no_package_manager():
    executor = FakeExecutor()

    assert pg_extensions.install_packages(executor, "rhel", [], "17") == []
    assert executor.commands == []


# ---------------------------------------------------------------------------
# what cannot work
# ---------------------------------------------------------------------------


def test_lolor_needs_postgres_16():
    selections = pg_extensions.parse("pgedge-lolor")

    with pytest.raises(ValueError, match="needs PostgreSQL 16"):
        pg_extensions.validate(selections, "15")
    assert pg_extensions.validate(selections, "16") is True
    assert pg_extensions.validate(selections, "17.11") is True


def test_a_beta_major_is_read_as_its_number():
    assert pg_extensions.validate(pg_extensions.parse("pgedge-lolor"),
                                  "19beta3") is True


def test_snowflake_cannot_identify_more_nodes_than_its_guc_allows():
    selections = pg_extensions.parse("pgedge-snowflake")

    with pytest.raises(ValueError, match="1..1023"):
        pg_extensions.validate(selections, "17", node_count=1024)


# ---------------------------------------------------------------------------
# node identity
# ---------------------------------------------------------------------------


def test_the_node_number_comes_from_the_node_name():
    plan = plan_with(("n1", "n2", "n3"))

    assert [pg_extensions.node_id(plan, n) for n in plan.spock_nodes] == [1, 2, 3]


def test_a_node_added_after_a_removal_does_not_reuse_a_live_number():
    """Position would: remove n2 and n3 slides to second, so the next node
    added is handed 3 — which n3 is already generating ids under."""
    plan = plan_with(("n1", "n3"))

    assert pg_extensions.node_id(plan, plan.node("n3")) == 3
    assert pg_extensions.node_id(plan, node("n4")) == 4


def test_a_standby_keeps_its_leaders_number():
    """It is a byte-for-byte copy, and a promotion must not change the number
    the ids issued before it were generated under."""
    plan = plan_with(("n1", "n2"), standbys=[("n1s1", "n1")])

    assert pg_extensions.node_id(plan, plan.node("n1s1")) == \
        pg_extensions.node_id(plan, plan.node("n1"))


def test_names_without_numbers_fall_back_to_position():
    plan = plan_with(("alpha", "beta"))

    assert [pg_extensions.node_id(plan, n) for n in plan.spock_nodes] == [1, 2]


def test_names_that_collide_on_their_number_fall_back_to_position():
    plan = plan_with(("a1", "b1"))

    assert sorted(pg_extensions.node_id(plan, n) for n in plan.spock_nodes) == [1, 2]


# ---------------------------------------------------------------------------
# building from source
# ---------------------------------------------------------------------------


def test_a_source_build_clones_the_ref_and_builds_with_pgxs():
    """Both repositories document exactly this: pg_config on PATH, then
    make USE_PGXS=1 and make USE_PGXS=1 install."""
    executor = FakeExecutor(replies={"pg_config --sharedir": (True, "/usr/share/pg")})
    spec = pg_extensions.CATALOG["lolor"]

    details = pg_extensions.build(executor, spec, "v1.2.0", "/usr/pgsql-17/bin")

    assert executor.ran("git clone --branch v1.2.0 --depth 1 "
                        "https://github.com/pgEdge/lolor.git")
    assert any("USE_PGXS=1 make" in c and "PATH=/usr/pgsql-17/bin:$PATH" in c
               for c in executor.commands)
    assert any("USE_PGXS=1 make install" in c for c in executor.commands)
    assert details["ref"] == "v1.2.0"


def test_a_build_that_installed_no_control_file_is_a_failure():
    """CREATE EXTENSION would fail much later, with nothing pointing here."""
    executor = FakeExecutor(replies={
        "pg_config --sharedir": (True, "/usr/share/pg"),
        "test -f /usr/share/pg/extension/lolor.control": (False, ""),
    })

    with pytest.raises(RuntimeError, match="nothing for CREATE EXTENSION"):
        pg_extensions.build(executor, pg_extensions.CATALOG["lolor"], "main",
                            "/usr/pgsql-17/bin")


def test_building_on_a_packaged_cluster_installs_the_headers_first():
    """pg_config and the server headers come from the -devel package; a
    source-built cluster already has both."""
    plan = plan_with(deploy_mode="packages")
    executor = FakeExecutor(replies={"pg_config --sharedir": (True, "/usr/share/pg")})
    selections = pg_extensions.parse("pgedge-lolor:source")

    pg_extensions.build_on_host(executor, plan, plan.hosts[0], selections)

    assert any("pgedge-postgresql17-devel" in c for c in executor.commands)


def test_building_on_a_source_cluster_does_not_reinstall_the_toolchain():
    plan = plan_with(deploy_mode="source")
    executor = FakeExecutor(replies={"pg_config --sharedir": (True, "/usr/share/pg")})
    selections = pg_extensions.parse("pgedge-lolor:source")

    pg_extensions.build_on_host(executor, plan, plan.hosts[0], selections)

    assert not any("devel" in c for c in executor.commands)


def test_nothing_is_built_when_every_entry_is_a_package():
    plan = plan_with()
    executor = FakeExecutor()

    assert pg_extensions.build_on_host(
        executor, plan, plan.hosts[0], pg_extensions.parse("pgedge-lolor")
    ) == {}
    assert executor.commands == []


# ---------------------------------------------------------------------------
# creating them in the database
# ---------------------------------------------------------------------------


def test_the_identity_guc_is_set_before_the_extension_is_created():
    """snowflake's default is invalid by design: an extension created without
    its GUC raises on the first nextval(), which an application discovers
    rather than the deployment."""
    plan = plan_with(("n1", "n2"), pg_extensions=pg_extensions.parse("pgedge-snowflake"))
    executor = FakeExecutor()

    created = pg_extensions.create_on_node(executor, plan, plan.node("n2"))

    assert created == ["snowflake"]
    guc = next(i for i, c in enumerate(executor.commands)
               if "ALTER SYSTEM SET snowflake.node" in c)
    extension = next(i for i, c in enumerate(executor.commands)
                     if "CREATE EXTENSION IF NOT EXISTS snowflake" in c)
    assert guc < extension


def test_each_node_is_given_its_own_number():
    plan = plan_with(("n1", "n2", "n3"),
                     pg_extensions=pg_extensions.parse("pgedge-lolor"))
    seen = []
    for target in plan.spock_nodes:
        executor = FakeExecutor()
        pg_extensions.create_on_node(executor, plan, target)
        seen.append(next(c for c in executor.commands
                         if "ALTER SYSTEM SET lolor.node" in c))

    assert "lolor.node = 1" in seen[0]
    assert "lolor.node = 2" in seen[1]
    assert "lolor.node = 3" in seen[2]


def test_a_cluster_with_no_extensions_creates_nothing():
    plan = plan_with()
    executor = FakeExecutor()

    assert pg_extensions.create_on_node(executor, plan, plan.node("n1")) == []
    assert executor.commands == []


def test_the_plan_summary_says_which_extensions_were_asked_for():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor:source@v1.2.0"))
    plain = plan_with()

    assert any("pgedge-lolor:source@v1.2.0" in line
               for line in plan.summary_lines())
    assert any("Extensions     : none" in line for line in plain.summary_lines())


# ---------------------------------------------------------------------------
# lolor's own tables
# ---------------------------------------------------------------------------


def test_lolors_tables_are_added_to_a_replication_set():
    """Without this lolor is installed and inert: large objects land in
    lolor.pg_largeobject and replicate nowhere."""
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor"))
    executor = FakeExecutor()

    added, problems = pg_extensions.replicate_tables(executor, plan,
                                                     plan.node("n1"))

    assert added == ["lolor.pg_largeobject", "lolor.pg_largeobject_metadata"]
    assert problems == []


def test_a_table_already_in_the_set_is_not_a_problem():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor"))
    executor = FakeExecutor(replies={
        "repset_add_table": (False, "ERROR: table is already in the set"),
    })

    added, problems = pg_extensions.replicate_tables(executor, plan,
                                                     plan.node("n1"))

    assert len(added) == 2 and problems == []


def test_snowflake_has_no_tables_of_its_own_to_replicate():
    """Its sequences live in the user's own tables, which Spock already
    carries."""
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-snowflake"))
    executor = FakeExecutor()

    assert pg_extensions.replicate_tables(executor, plan, plan.node("n1")) == ([], [])
    assert executor.commands == []


def test_a_bad_value_is_a_usage_error_not_a_traceback():
    """It is caught before anything is touched, so the message has to say what
    is wrong rather than where it broke."""
    import argparse
    from deployment import cli

    args = argparse.Namespace(pg_extensions="pgedge-timescale", mode="packages",
                              pg_version="", pg_major="17", nodes=2)

    with pytest.raises(SystemExit) as raised:
        cli._extensions(args)
    assert raised.value.code == cli.EXIT_USAGE


# ---------------------------------------------------------------------------
# ACE — a tool beside the cluster, not an extension inside it
# ---------------------------------------------------------------------------


def test_ace_has_one_package_name_for_every_server_and_both_families():
    """It talks to the cluster from outside, so nothing about it is tied to a
    PostgreSQL major — unlike the extensions, which are built into one."""
    ace = pg_extensions.CATALOG["ace"]

    assert ace.package("rhel", "17") == "pgedge-ace"
    assert ace.package("deb", "17") == "pgedge-ace"
    assert ace.package("rhel", "18") == "pgedge-ace"
    assert ace.is_tool


def test_ace_is_selected_like_the_others():
    assert pg_extensions.parse("pgedge-ace") == [
        {"name": "ace", "mode": "packages", "ref": ""}
    ]
    assert pg_extensions.parse("ace:source@v1.0.0") == [
        {"name": "ace", "mode": "source", "ref": "v1.0.0"}
    ]


def test_ace_has_no_postgres_version_floor():
    assert pg_extensions.validate(pg_extensions.parse("pgedge-ace"), "15") is True


def test_building_ace_uses_go_and_installs_the_binary_on_path():
    """ACE's README: install Go, `go build -o ace ./cmd/ace/`, then put the
    binary somewhere on PATH."""
    executor = FakeExecutor(replies={"go version": (True, "go version go1.26.1 linux/amd64")})

    details = pg_extensions.build(executor, pg_extensions.CATALOG["ace"],
                                  "v1.0.0", "/usr/pgsql-17/bin")

    assert executor.ran("git clone --branch v1.0.0 --depth 1 "
                        "https://github.com/pgEdge/ace.git")
    assert any("go build -o ace ./cmd/ace/" in c for c in executor.commands)
    assert executor.ran("install -m 0755 /opt/pgedge/build/ace/ace "
                        "/usr/local/bin/ace")
    assert details["binary"] == "/usr/local/bin/ace"
    # Nothing about PostgreSQL: no PGXS, no pg_config, no control file.
    assert not any("USE_PGXS" in c or "pg_config" in c for c in executor.commands)


def test_a_host_with_a_new_enough_go_keeps_it():
    executor = FakeExecutor(replies={"go version": (True, "go version go1.27.0 linux/amd64")})

    assert pg_extensions.ensure_go(executor) == "1.27.0"
    assert not any("go.dev/dl" in c for c in executor.commands)


def test_a_host_with_no_go_gets_the_official_release():
    """No distribution ships Go 1.26 yet, so the release tarball is the only
    way to get one."""
    executor = FakeExecutor(replies={"go version": (False, "")})

    pg_extensions.ensure_go(executor, arch="aarch64")

    assert any("go.dev/dl/go" in c and "linux-arm64.tar.gz" in c
               for c in executor.commands)
    assert executor.ran("tar -C /usr/local -xzf")


def test_a_host_with_an_old_go_is_upgraded():
    executor = FakeExecutor(replies={"go version": (True, "go version go1.21.5 linux/amd64")})

    pg_extensions.ensure_go(executor)

    assert any("go.dev/dl/go" in c for c in executor.commands)


def test_a_built_ace_that_cannot_run_is_a_failure():
    executor = FakeExecutor(replies={
        "go version": (True, "go version go1.26.1 linux/amd64"),
        "/usr/local/bin/ace --version": (False, "cannot execute"),
    })

    with pytest.raises(RuntimeError, match="does not run"):
        pg_extensions.build(executor, pg_extensions.CATALOG["ace"], "main",
                            "/usr/pgsql-17/bin")


def test_ace_is_never_created_in_a_database():
    """There is no CREATE EXTENSION ace, and no node identity to give it."""
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace"))
    executor = FakeExecutor()

    assert pg_extensions.create_on_node(executor, plan, plan.node("n1")) == []
    assert pg_extensions.installed_versions(executor, plan, plan.node("n1")) == {}
    assert pg_extensions.node_identities(executor, plan, plan.node("n1")) == {}
    assert executor.commands == []


def test_a_mixed_selection_creates_only_the_in_database_half():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace,pgedge-snowflake"))
    executor = FakeExecutor()

    assert pg_extensions.create_on_node(executor, plan, plan.node("n1")) == \
        ["snowflake"]


def test_an_installed_tool_is_reported_by_asking_the_binary():
    """A tool leaves no row in pg_extension; the binary answering is the only
    evidence it is there."""
    executor = FakeExecutor(replies={"ace --version": (True, "ace 1.0.0\n")})

    versions = pg_extensions.tool_versions(
        executor, selections=pg_extensions.parse("pgedge-ace")
    )

    assert versions == {"ace": "ace 1.0.0"}


def test_building_ace_alone_does_not_install_postgres_headers():
    """It compiles against nothing of PostgreSQL's."""
    plan = plan_with(deploy_mode="packages")
    executor = FakeExecutor(replies={"go version": (True, "go version go1.26.1 linux/amd64")})

    pg_extensions.build_on_host(executor, plan, plan.hosts[0],
                                pg_extensions.parse("pgedge-ace:source"))

    assert not any("devel" in c for c in executor.commands)


def test_a_mixed_build_still_installs_the_postgres_toolchain():
    plan = plan_with(deploy_mode="packages")
    executor = FakeExecutor(replies={
        "go version": (True, "go version go1.26.1 linux/amd64"),
        "pg_config --sharedir": (True, "/usr/share/pg"),
    })

    pg_extensions.build_on_host(
        executor, plan, plan.hosts[0],
        pg_extensions.parse("pgedge-ace:source,pgedge-lolor:source"),
    )

    assert any("pgedge-postgresql17-devel" in c for c in executor.commands)
    assert any("go build" in c for c in executor.commands)
