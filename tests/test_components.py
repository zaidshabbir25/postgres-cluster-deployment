#!/usr/bin/env python3
"""The component table in the plan summary.

Two questions it exists to answer before anything is touched: which version of
each piece is this going to be, and where will it end up. Neither has one
answer — both turn on packages versus source, and for packages the honest
answer about a version is "whatever the channel is carrying today". Saying that
plainly is the point; a confident wrong number would be worse than none.
"""

from aspects import components, pg_extensions
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK


def plan_with(mode="packages", family="", **kwargs):
    host = Host(name="h0", address="10.0.1.10")
    host.family = family
    plan = ClusterPlan(cluster_name="pgedge", pg_major="17",
                       deploy_mode=mode, hosts=[host], **kwargs)
    plan.nodes = [Node(name="n1", role=ROLE_SPOCK, host="h0",
                       address="10.0.1.10", pg_port=5432, restapi_port=8008,
                       data_dir="/d", scope="s", config_file="c",
                       pgpass_file="p")]
    return plan


def row(plan, name):
    return next(r for r in components.rows(plan) if r["name"] == name)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def test_a_packaged_version_is_labelled_as_a_pin_not_a_promise():
    """configuration/config17.env is a reference point: the deployment
    installs what the channel offers and compares afterwards."""
    plan = plan_with()

    assert "(expected)" in row(plan, "PostgreSQL")["version"]
    assert "(expected)" in row(plan, "spock50")["version"]


def test_an_exact_version_is_labelled_as_one():
    plan = plan_with(pg_version="17.9")

    assert row(plan, "PostgreSQL")["version"] == "17.9 (exact)"


def test_a_source_build_shows_the_ref_it_will_build():
    plan = plan_with("source", source_build={"pg_version": "17.11",
                                             "spock_branch": "v5.0.5"})

    assert row(plan, "PostgreSQL")["version"] == "17.11 (exact)"
    assert row(plan, "spock50")["version"] == "v5.0.5 (branch or tag)"


def test_a_packaged_extension_version_is_not_knowable_in_advance():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor"))

    assert row(plan, "lolor")["version"] == "channel decides"


def test_a_built_extension_shows_its_ref():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor:source@v1.2.0"))

    assert row(plan, "lolor")["version"] == "v1.2.0 (branch or tag)"


# ---------------------------------------------------------------------------
# where things land
# ---------------------------------------------------------------------------


def test_before_the_hosts_are_probed_both_prefixes_are_named():
    """The two families disagree about this path, and guessing one would be
    wrong half the time."""
    plan = plan_with()

    location = row(plan, "PostgreSQL")["location"]
    assert "/usr/pgsql-17 (RHEL)" in location
    assert "/usr/lib/postgresql/17 (Debian)" in location


def test_a_probed_host_gets_the_one_path_that_applies():
    assert row(plan_with(family="rhel"), "PostgreSQL")["location"] == "/usr/pgsql-17"
    assert row(plan_with(family="deb"), "PostgreSQL")["location"] == \
        "/usr/lib/postgresql/17"


def test_a_source_build_installs_to_its_own_prefix():
    plan = plan_with("source", source_build={"pg_version": "17.11"})

    assert row(plan, "PostgreSQL")["location"] == "/opt/pgedge/pg17"
    assert row(plan, "Patroni")["location"] == "/opt/pgedge/patroni-venv"


def test_everything_in_the_prefix_points_at_it_rather_than_repeating_it():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor,pgedge-snowflake"))

    for name in ("spock50", "lolor", "snowflake"):
        assert row(plan, name)["location"] == components.IN_PREFIX


def test_ace_lands_where_binaries_go_not_in_a_postgres_prefix():
    packaged = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace"))
    built = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace:source"))

    assert row(packaged, "ace")["location"] == "/usr/bin/ace"
    assert row(built, "ace")["location"].startswith("/usr/local/bin/ace")
    assert "/usr/local/go" in row(built, "ace")["location"]


# ---------------------------------------------------------------------------
# where things come from
# ---------------------------------------------------------------------------


def test_an_extension_package_is_named_for_both_families_until_one_is_known():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor"))

    origin = row(plan, "lolor")["origin"]
    assert "pgedge-lolor_17 (RHEL)" in origin
    assert "pgedge-postgresql-17-lolor (Debian)" in origin


def test_a_package_named_the_same_on_both_families_is_named_once():
    """ACE is version-independent, so there is no pair to print."""
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace"))

    assert row(plan, "ace")["origin"].endswith("— pgedge-ace")


def test_the_channel_is_named_because_it_decides_what_arrives():
    plan = plan_with(repo_channel="staging")

    assert "staging channel" in row(plan, "PostgreSQL")["origin"]


def test_a_built_component_names_its_repository():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-snowflake:source"))

    assert row(plan, "snowflake")["origin"] == \
        "https://github.com/pgEdge/snowflake.git"


# ---------------------------------------------------------------------------
# the rendered table
# ---------------------------------------------------------------------------


def test_the_summary_lists_every_component_with_a_version_and_a_location():
    plan = plan_with(pg_extensions=pg_extensions.parse(
        "pgedge-lolor,pgedge-snowflake,pgedge-ace"))

    text = "\n".join(plan.summary_lines())

    for name in ("PostgreSQL", "spock50", "Patroni", "etcd",
                 "lolor", "snowflake", "ace"):
        assert name in text
    assert "COMPONENT" in text and "ORIGIN / INSTALLS TO" in text


def test_the_build_directory_is_named_only_when_something_is_built():
    built = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace:source"))
    packaged = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace"))

    assert components.builds_from_source(built)
    assert not components.builds_from_source(packaged)
    assert "/opt/pgedge/build" in "\n".join(components.summary_lines(built))
    assert "/opt/pgedge/build" not in "\n".join(components.summary_lines(packaged))


def test_a_cluster_with_no_add_ons_still_lists_the_core_four():
    plan = plan_with()

    names = [r["name"] for r in components.rows(plan)]

    assert names == ["PostgreSQL", "spock50", "Patroni", "etcd"]


def test_the_server_package_is_not_the_contrib_one():
    """server_packages() leads with Spock on purpose — installing it drags in
    the matching server — so the first and last entries are both the wrong
    answer to "where does PostgreSQL come from"."""
    assert "contrib" not in row(plan_with(family="rhel"), "PostgreSQL")["origin"]
    assert "pgedge-postgresql17" in row(plan_with(family="rhel"), "PostgreSQL")["origin"]
    assert "pgedge-postgresql-17" in row(plan_with(family="deb"), "PostgreSQL")["origin"]
