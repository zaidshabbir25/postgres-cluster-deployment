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


def test_before_the_hosts_are_probed_one_family_is_shown_and_declared():
    """The two families disagree about every path and half the package names.
    Printing both in every cell turns the table into a wall, so one is shown
    and the assumption is stated above it — never hidden."""
    plan = plan_with()

    family, assumed = components.shown_family(plan)

    assert (family, assumed) == (components.ASSUMED_FAMILY, True)
    assert row(plan, "PostgreSQL")["location"] == "/usr/pgsql-17"
    assert "RHEL ones" in "\n".join(components.summary_lines(plan))


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


def test_an_extension_package_is_named_for_the_family_being_shown():
    unprobed = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor"))
    debian = plan_with(family="deb",
                       pg_extensions=pg_extensions.parse("pgedge-lolor"))

    assert row(unprobed, "lolor")["origin"].endswith("pgedge-lolor_17")
    assert row(debian, "lolor")["origin"].endswith("pgedge-postgresql-17-lolor")


def test_a_package_named_the_same_on_both_families_is_named_once():
    """ACE is version-independent, so there is no pair to print."""
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace"))

    assert row(plan, "ace")["origin"].endswith("— pgedge-ace")


def test_the_channel_is_named_because_it_decides_what_arrives():
    plan = plan_with(repo_channel="staging")

    assert "staging channel" in row(plan, "PostgreSQL")["origin"]


def test_a_built_component_names_its_repository():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-snowflake:source"))

    # The scheme and .git are dropped: the column is long enough already.
    assert row(plan, "snowflake")["origin"] == "github.com/pgEdge/snowflake"


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
    assert "COMPONENT" in text
    assert "SOURCE" in text and "INSTALLS TO" in text


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


# ---------------------------------------------------------------------------
# the table itself
# ---------------------------------------------------------------------------


def test_the_table_has_a_rule_and_aligned_columns():
    plan = plan_with(family="rhel")

    lines = components.summary_lines(plan)
    header, rule = lines[0], lines[1]

    assert set(rule) <= {"-", " "}
    # The rule spans the table, which is the longest row — not the header,
    # whose last cell is usually shorter than its column.
    assert len(rule) == max(len(line) for line in lines)
    assert len(rule) >= len(header)
    # Every column starts at the same offset on every row.
    for column in ("VERSION", "SOURCE", "INSTALLS TO"):
        start = header.index(column)
        body = [line for line in lines[2:] if line.strip() and not
                line.startswith(" ")]
        assert all(line[start - 1] == " " for line in body)


def test_a_value_too_long_for_a_cell_becomes_a_note():
    """Stretching the table past a terminal's width makes it unreadable, which
    defeats the point of a table."""
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-ace:source"))

    text = "\n".join(components.summary_lines(plan))

    assert "/usr/local/bin/ace [1]" in text
    assert "[1] Go toolchain in /usr/local/go" in text
    table = [line for line in components.summary_lines(plan)
             if line and not line.startswith(" ")]
    assert all(len(line) < 120 for line in table)


def test_a_build_gets_no_platform_caveat_because_none_applies():
    """It installs to /opt/pgedge whatever the distribution is, and names no
    packages — a caveat there would be a caveat about nothing."""
    built = plan_with("source", source_build={"pg_version": "18.6"})
    mixed = plan_with("source", source_build={"pg_version": "18.6"},
                      pg_extensions=pg_extensions.parse("pgedge-lolor:packages"))

    assert "RHEL ones" not in "\n".join(components.summary_lines(built))
    assert "RHEL ones" in "\n".join(components.summary_lines(mixed))


def test_a_source_build_prefix_follows_the_version_not_the_major_flag():
    """build_major() takes it from the exact version, so --pg-version 18.6
    builds pg18 even when --pg-major still says 17. A summary naming pg17 would
    point at a directory the deployment never creates."""
    plan = plan_with("source", source_build={"pg_version": "18.6"})

    assert components.built_major(plan) == "18"
    assert row(plan, "PostgreSQL")["location"] == "/opt/pgedge/pg18"
