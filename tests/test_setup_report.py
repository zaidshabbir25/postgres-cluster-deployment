#!/usr/bin/env python3
"""The summary printed when a deployment finishes.

The plan summary says what is about to happen; this says what happened, in the
same shape so the two read against each other. The judgements worth testing are
about attribution: one step installs several things, so its failure does not
belong to all of them, and a component the run never reached is not broken.
"""

from aspects import pg_extensions, setup_report
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK, ROLE_STANDBY


def node(name, role=ROLE_SPOCK, leader=None, scope=None, standbys=()):
    return Node(name=name, role=role, host="local", address="localhost",
                pg_port=5432, restapi_port=8008, data_dir="/d",
                scope=scope or f"pgedge-{name}", config_file="c",
                pgpass_file="p", leader=leader, standbys=list(standbys))


def plan_with(mode="source", standby=False, extensions="", **kwargs):
    host = Host(name="local", address="localhost", local=True)
    host.family = "rhel"
    plan = ClusterPlan(
        cluster_name="pgedge", pg_major="18", pg_version="18.6",
        deploy_mode=mode, hosts=[host],
        source_build={"pg_version": "18.6", "spock_branch": "v5_STABLE"},
        pg_extensions=pg_extensions.parse(extensions) if extensions else [],
        **kwargs,
    )
    plan.nodes = [node("n1", standbys=["n1s1"] if standby else []), node("n2")]
    if standby:
        plan.nodes.append(node("n1s1", ROLE_STANDBY, leader="n1",
                               scope="pgedge-n1"))
    return plan


def step(name, status="passed", message=""):
    return {"name": name, "status": status, "message": message,
            "duration": 1.0, "detail": ""}


def result_with(plan, steps, results=None):
    return {"plan": plan, "steps": steps, "results": results or {},
            "log_dir": "/logs/run"}


def row_for(result, name):
    return next(r for r in setup_report.component_rows(result) if r["name"] == name)


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


def test_every_component_gets_a_row_with_a_verdict():
    plan = plan_with(extensions="pgedge-lolor:source,pgedge-ace:source")
    result = result_with(plan, [step("Build PostgreSQL and Spock from source"),
                                step("Install optional extensions")])

    names = [r["name"] for r in setup_report.component_rows(result)]

    assert names == ["PostgreSQL", "spock50", "Patroni", "etcd", "lolor", "ace"]


def test_a_component_the_run_never_reached_is_not_called_broken():
    """"not reached" and "broken" lead somewhere different."""
    plan = plan_with(extensions="pgedge-lolor:source")
    result = result_with(plan, [
        step("Build PostgreSQL and Spock from source", "failed", "make error"),
    ])

    assert row_for(result, "PostgreSQL")["status"] == setup_report.FAILED
    assert row_for(result, "lolor")["status"] == setup_report.NOT_REACHED


def test_one_failing_step_does_not_condemn_everything_it_installed():
    """Create optional extensions handles both; snowflake reported its version,
    so it is there whatever lolor did."""
    plan = plan_with(extensions="pgedge-lolor:source,pgedge-snowflake:source")
    result = result_with(
        plan,
        [step("Install optional extensions"),
         step("Create optional extensions", "failed", "lolor.control missing")],
        {"extension_versions": {"snowflake": "2.0"}},
    )

    assert row_for(result, "snowflake")["status"] == setup_report.OK
    assert row_for(result, "snowflake")["version"] == "2.0"
    assert row_for(result, "lolor")["status"] == setup_report.FAILED


def test_an_asked_for_postgres_version_is_not_evidence_that_it_built():
    """--pg-version puts a number in the plan before anything is compiled."""
    plan = plan_with("source")
    result = result_with(plan, [
        step("Build PostgreSQL and Spock from source", "failed", "make error"),
    ])

    assert plan.pg_version == "18.6"
    assert row_for(result, "PostgreSQL")["status"] == setup_report.FAILED


def test_a_failed_row_names_the_step_and_the_message():
    plan = plan_with()
    result = result_with(plan, [
        step("Build PostgreSQL and Spock from source", "failed", "make error"),
    ])

    assert "make error" in row_for(result, "PostgreSQL")["detail"]
    assert "Build PostgreSQL" in row_for(result, "PostgreSQL")["detail"]


def test_the_failure_points_at_the_logs():
    plan = plan_with()
    result = result_with(plan, [
        step("Build PostgreSQL and Spock from source", "failed", "make error"),
    ])

    text = "\n".join(setup_report.lines(result))

    assert "/logs/run/deploy.log" in text
    assert "/logs/run/<node>.log" in text


def test_a_clean_run_does_not_mention_logs():
    plan = plan_with()
    result = result_with(plan, [step("Build PostgreSQL and Spock from source")])

    assert "deploy.log" not in "\n".join(setup_report.lines(result))


def test_one_failing_step_produces_one_note_not_one_per_row():
    plan = plan_with()
    result = result_with(plan, [
        step("Build PostgreSQL and Spock from source", "failed", "make error"),
    ])

    notes = [line for line in setup_report.lines(result)
             if line.strip().startswith("[")]

    assert len(notes) == 1


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------


def test_a_version_nothing_confirmed_is_marked_as_only_the_plan():
    plan = plan_with(extensions="pgedge-lolor:source@v1.2.3")
    result = result_with(plan, [step("Install optional extensions")])

    assert row_for(result, "lolor")["version"] == "v1.2.3 (planned)"


def test_a_version_the_run_reported_is_shown_bare():
    plan = plan_with()
    result = result_with(plan, [step("Create Spock extensions")],
                         {"spock_versions": {"n1": "5.0.11"}})

    assert row_for(result, "spock50")["version"] == "5.0.11"


def test_a_tools_own_name_is_stripped_from_its_version():
    """The column already says which tool this is."""
    plan = plan_with(extensions="pgedge-ace:source")
    result = result_with(
        plan, [step("Install optional extensions")],
        {"pg_extensions": {"local": {"tools": {"ace": "ace version 1.0.3"}}}},
    )

    assert row_for(result, "ace")["version"] == "1.0.3"


def test_patroni_and_etcd_are_told_apart_in_the_package_inventory():
    """pgedge-patroni-etcd contains both words; a substring match would report
    Patroni's version for etcd."""
    plan = plan_with("packages")
    result = result_with(
        plan, [step("Install Patroni and etcd packages")],
        {"packages": {"local": [{"package": "pgedge-patroni-etcd",
                                 "version": "4.1.3-1"},
                                {"package": "pgedge-etcd", "version": "3.6.8-1"}]}},
    )

    assert row_for(result, "Patroni")["version"] == "4.1.3-1"
    assert row_for(result, "etcd")["version"] == "3.6.8-1"


def test_the_source_column_carries_the_ref_that_was_built():
    plan = plan_with(extensions="pgedge-lolor:source@v1.2.3")
    result = result_with(plan, [step("Install optional extensions")])

    assert row_for(result, "lolor")["source"].endswith("lolor@v1.2.3")


# ---------------------------------------------------------------------------
# replication
# ---------------------------------------------------------------------------


def test_cross_wiring_counts_the_nodes_and_the_subscriptions():
    plan = plan_with()
    result = result_with(plan, [step("Cross-wire Spock nodes")])

    label, detail, status = setup_report.replication_rows(result)[0]

    assert label == "Cross-wiring"
    assert "2 Spock nodes, 2 subscriptions" in detail
    assert status == setup_report.OK


def test_a_single_node_cluster_has_nothing_to_cross_wire():
    plan = plan_with()
    plan.nodes = [node("n1")]
    result = result_with(plan, [])

    assert setup_report.replication_rows(result)[0][2] == setup_report.SKIPPED


def test_each_standby_pair_is_listed_with_its_mode_and_live_state():
    plan = plan_with(standby=True, synchronous_mode="on")
    result = result_with(
        plan, [step("Add standby nodes")],
        {"health": {"scopes": {"pgedge-n1": {"members": [
            {"name": "n1", "role": "Leader", "state": "running"},
            {"name": "n1s1", "role": "Sync Standby", "state": "streaming"},
        ]}}}},
    )

    pair = next(r for r in setup_report.replication_rows(result)
                if "n1s1" in r[0])

    assert pair[0].strip() == "n1 -> n1s1"
    assert "synchronous" in pair[1] and "streaming" in pair[1]
    assert pair[2] == setup_report.OK


def test_an_asynchronous_scope_says_so():
    plan = plan_with(standby=True)
    result = result_with(
        plan, [step("Add standby nodes")],
        {"health": {"scopes": {"pgedge-n1": {"members": [
            {"name": "n1s1", "role": "Replica", "state": "streaming"},
        ]}}}},
    )

    pair = next(r for r in setup_report.replication_rows(result)
                if "n1s1" in r[0])

    assert "asynchronous" in pair[1]


def test_a_standby_that_was_never_created_is_not_reported_as_failing():
    """Patroni was never asked to create it; the earlier failure is the story."""
    plan = plan_with(standby=True)
    result = result_with(plan, [
        step("Build PostgreSQL and Spock from source", "failed", "make error"),
    ])

    rows = setup_report.standby_rows(result)

    assert all(status == setup_report.NOT_REACHED for _, _, status in rows)


def test_a_standby_that_is_not_streaming_is_reported_as_failing():
    plan = plan_with(standby=True)
    result = result_with(
        plan, [step("Add standby nodes")],
        {"health": {"scopes": {"pgedge-n1": {"members": [
            {"name": "n1s1", "role": "Replica", "state": "start failed"},
        ]}}}},
    )

    pair = next(r for r in setup_report.standby_rows(result) if "n1s1" in r[0])

    assert pair[2] == setup_report.FAILED


def test_a_cluster_with_no_standbys_says_none_were_asked_for():
    plan = plan_with()
    result = result_with(plan, [step("Cross-wire Spock nodes")])

    assert ("Standby nodes", "none requested", setup_report.SKIPPED) in \
        setup_report.replication_rows(result)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_a_cancelled_run_has_nothing_to_report():
    assert setup_report.lines({"steps": [], "results": {}}) == []


def test_the_report_is_a_table_with_both_sections():
    plan = plan_with(standby=True, extensions="pgedge-ace:source")
    result = result_with(plan, [step("Install optional extensions"),
                                step("Cross-wire Spock nodes"),
                                step("Add standby nodes")])

    text = "\n".join(setup_report.lines(result))

    assert "PostgreSQL Cluster Setup Summary" in text
    assert "COMPONENT" in text and "BUILT / INSTALLED FROM" in text
    assert "Replication" in text and "Cross-wiring" in text


def test_a_quorum_scope_is_named_as_one():
    plan = plan_with(standby=True, synchronous_mode="quorum")
    result = result_with(
        plan, [step("Add standby nodes")],
        {"health": {"scopes": {"pgedge-n1": {"members": [
            {"name": "n1s1", "role": "Quorum Standby", "state": "streaming"},
        ]}}}},
    )

    pair = next(r for r in setup_report.replication_rows(result)
                if "n1s1" in r[0])

    assert "quorum" in pair[1]
