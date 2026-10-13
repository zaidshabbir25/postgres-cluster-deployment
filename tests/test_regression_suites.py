#!/usr/bin/env python3
"""Running each component's upstream regression suite.

The four suites have almost nothing in common, and the differences are the
point: two are pg_regress against a live server, one is a Python harness built
for a different directory layout, and one starts its own containers and never
looks at the deployed cluster. What is worth testing is that each is run the
way its own project intends, that a suite which cannot run here says why rather
than failing, and that the report does not imply ACE checked the cluster.
"""

import pytest

from aspects import pg_extensions
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK, ROLE_STANDBY
from deployment import health_check
from regression import runner as regression
from tests.conftest import FakeExecutor, RecordingLogger

PGXS = "/usr/lib/pgsql/pgxs/src/makefiles/pgxs.mk"


def node(name, port=5432, role=ROLE_SPOCK, leader=None):
    return Node(name=name, role=role, host="local", address="10.0.1.10",
                pg_port=port, restapi_port=8008, data_dir=f"/var/lib/pgedge/{name}",
                scope=f"pgedge-{name}", config_file="c", pgpass_file="p",
                bin_dir="/usr/pgsql-17/bin", leader=leader)


def plan_with(names=("n1", "n2"), ports=(5432, 5433), **kwargs):
    host = Host(name="local", address="10.0.1.10")
    host.family = "rhel"
    plan = ClusterPlan(cluster_name="pgedge", pg_major="17", pg_version="17.11",
                       hosts=[host], **kwargs)
    plan.nodes = [node(name, port) for name, port in zip(names, ports)]
    return plan


def ready(**extra):
    """An executor on which every prerequisite is satisfied."""
    replies = {
        "pg_config --pgxs": (True, PGXS),
        f"test -f {PGXS}": (True, ""),
        "FROM pg_extension WHERE extname": (True, "1\n"),
        "docker info": (True, ""),
    }
    replies.update(extra)
    return FakeExecutor(replies=replies)


# ---------------------------------------------------------------------------
# choosing what to run
# ---------------------------------------------------------------------------


def test_all_four_components_are_known():
    assert regression.known_components() == ["ace", "lolor", "snowflake", "spock"]


def test_the_component_list_is_read_in_the_order_it_was_given():
    assert regression.parse_components("spock,lolor") == ["spock", "lolor"]
    assert regression.parse_components("all") == regression.known_components()
    assert regression.parse_components("") == []


def test_package_names_and_spock_majors_resolve_to_their_suite():
    assert regression.parse_components("pgedge-lolor,spock50") == ["lolor", "spock"]


def test_a_component_with_no_suite_is_refused_by_name():
    with pytest.raises(ValueError, match="unknown component 'patroni'"):
        regression.parse_components("patroni")


def test_each_component_works_in_its_own_directory():
    """One failing suite must not disturb another's artefacts."""
    workspaces = {name: regression.suite_for(name).workspace()
                  for name in regression.known_components()}

    assert len(set(workspaces.values())) == len(workspaces)
    assert all(path.startswith(regression.REGRESSION_ROOT)
               for path in workspaces.values())


# ---------------------------------------------------------------------------
# which revision gets tested
# ---------------------------------------------------------------------------


def test_spocks_tests_come_from_the_branch_the_extension_was_built_from():
    """A newer suite tests behaviour an older extension does not have, and the
    failures are about the skew rather than the cluster."""
    plan = plan_with(source_build={"spock_branch": "v5.0.5"})

    assert health_check.ref_for(plan, "spock") == "v5.0.5"


def test_an_extensions_tests_follow_the_ref_it_was_built_from():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor:source@v1.2.3"))

    assert health_check.ref_for(plan, "lolor") == "v1.2.3"


def test_a_packaged_extension_falls_back_to_the_default_branch():
    plan = plan_with(pg_extensions=pg_extensions.parse("pgedge-lolor"))

    assert health_check.ref_for(plan, "lolor") == \
        pg_extensions.CATALOG["lolor"].default_ref


# ---------------------------------------------------------------------------
# spock and lolor: pg_regress against the live cluster
# ---------------------------------------------------------------------------


def test_spock_runs_installcheck_against_the_running_node():
    """`make check` builds a temporary instance, which says nothing about this
    cluster. `installcheck` points pg_regress at a live server."""
    plan = plan_with()
    executor = ready(**{"make installcheck": (True, "All 23 tests passed.")})

    result = regression.suite_for("spock").run(
        executor, plan, plan.node("n1"), "v5_STABLE", run_logger=RecordingLogger()
    )

    assert result.status == regression.PASSED
    command = next(c for c in executor.commands if "make installcheck" in c)
    assert "USE_PGXS=1" in command
    assert "PGPORT=5432" in command and "PGHOST=10.0.1.10" in command
    assert "make check" not in command


def test_the_suite_is_cloned_at_the_ref_into_its_own_workspace():
    plan = plan_with()
    executor = ready(**{"make installcheck": (True, "All 1 tests passed.")})

    regression.suite_for("lolor").run(executor, plan, plan.node("n1"), "v1.2.3")

    assert executor.ran("git clone --branch v1.2.3 --depth 1 "
                        "https://github.com/pgEdge/lolor.git "
                        "/opt/pgedge/regression/lolor/src")


@pytest.mark.parametrize("output,expected", [
    ("All 23 tests passed.", {"total": 23, "passed": 23, "failed": 0}),
    ("1 of 23 tests failed.", {"total": 23, "passed": 22, "failed": 1}),
    ("make: nothing to be done", {}),
])
def test_pg_regress_reports_its_own_tally(output, expected):
    assert regression.parse_pg_regress(output) == expected


def test_a_failing_regression_keeps_the_diffs():
    """regression.diffs is the file that says what actually differed."""
    plan = plan_with()
    executor = ready(**{
        "make installcheck": (False, "1 of 23 tests failed."),
        "test -e /opt/pgedge/regression/spock/src/regression.diffs": (True, ""),
    })

    result = regression.suite_for("spock").run(executor, plan, plan.node("n1"),
                                               "v5_STABLE")

    assert result.status == regression.FAILED
    assert result.tests == {"total": 23, "passed": 22, "failed": 1}
    assert any("regression.diffs" in path for path in result.artefacts)


def test_the_suites_own_tally_outranks_its_exit_status():
    """A harness that reports no failures and still exits non-zero has a
    problem with its own teardown, not with the cluster."""
    plan = plan_with()
    executor = ready(**{"make installcheck": (False, "All 23 tests passed.")})

    result = regression.suite_for("spock").run(executor, plan, plan.node("n1"),
                                               "v5_STABLE")

    assert result.status == regression.PASSED


def test_a_host_without_the_devel_package_is_skipped_not_failed():
    """pg_regress ships with the development files, so a packaged host can
    have psql and the server and still not have it."""
    plan = plan_with()
    executor = FakeExecutor(replies={"pg_config --pgxs": (False, "")})

    result = regression.suite_for("spock").run(executor, plan, plan.node("n1"),
                                               "v5_STABLE")

    assert result.status == regression.SKIPPED
    assert "pgxs" in result.detail
    assert not executor.ran("git clone")


def test_lolor_is_skipped_when_the_extension_was_never_created():
    """This tool creates it after cross-wiring; a cluster where that was
    skipped should say so rather than produce a wall of failures."""
    plan = plan_with()
    executor = ready(**{"FROM pg_extension WHERE extname": (True, "")})

    result = regression.suite_for("lolor").run(executor, plan, plan.node("n1"),
                                               "main")

    assert result.status == regression.SKIPPED
    assert "lolor extension" in result.detail


# ---------------------------------------------------------------------------
# snowflake: the harness expects a different layout
# ---------------------------------------------------------------------------


def test_snowflake_gets_a_shim_for_the_path_it_builds_itself():
    """runner.py takes everything else from EDGE_* variables, but builds the
    psql path as $EDGE_CLUSTER_DIR/n1/pgedge/$EDGE_COMPONENT/bin/psql."""
    plan = plan_with()
    executor = ready(**{"runner.py": (True, "PASS test_one\nPASS test_two")})

    result = regression.suite_for("snowflake").run(executor, plan,
                                                   plan.node("n1"), "main")

    assert result.status == regression.PASSED
    assert executor.ran("ln -sfn /usr/pgsql-17/bin "
                        "/opt/pgedge/regression/snowflake/cluster/n1/pgedge/pg17/bin")
    command = next(c for c in executor.commands if "runner.py" in c)
    assert "EDGE_CLUSTER_DIR=/opt/pgedge/regression/snowflake/cluster" in command
    assert "EDGE_START_PORT=5432" in command


def test_snowflake_says_so_when_the_second_node_is_not_on_the_next_port():
    """The harness takes n2's port as n1's + 1; where it is not, the run would
    quietly test one node twice."""
    plan = plan_with(ports=(5432, 6000))
    executor = ready(**{"runner.py": (True, "PASS one")})
    log = RecordingLogger()

    result = regression.suite_for("snowflake").run(executor, plan,
                                                   plan.node("n1"), "main",
                                                   run_logger=log)

    assert "port + 1" in result.output
    assert log.said("port + 1")


def test_snowflake_needs_two_spock_nodes():
    plan = plan_with(names=("n1",), ports=(5432,))
    executor = ready()

    result = regression.suite_for("snowflake").run(executor, plan,
                                                   plan.node("n1"), "main")

    assert result.status == regression.SKIPPED
    assert "two Spock nodes" in result.detail


# ---------------------------------------------------------------------------
# ace: tests itself, not the cluster
# ---------------------------------------------------------------------------


def test_ace_is_marked_as_not_testing_the_deployed_cluster():
    """Its integration tests start their own three-node cluster with
    testcontainers; a green result says nothing about this one."""
    assert regression.suite_for("ace").targets_cluster is False
    assert regression.suite_for("spock").targets_cluster is True


def test_ace_runs_go_test_with_the_cache_defeated():
    """A cached pass from a previous run would report success without starting
    a single container."""
    plan = plan_with()
    executor = ready(**{"go test": (True, "--- PASS: TestOne\n--- PASS: TestTwo\nok")})

    result = regression.suite_for("ace").run(executor, plan, plan.node("n1"),
                                             "main")

    assert result.status == regression.PASSED
    command = next(c for c in executor.commands if "go test" in c)
    assert "-count=1" in command
    assert "./tests/integration/..." in command


def test_ace_counts_what_go_test_reported():
    suite = regression.suite_for("ace")
    output = "--- PASS: TestA\n--- FAIL: TestB\n--- SKIP: TestC\nFAIL"

    assert suite.parse(output) == {"total": 3, "passed": 1, "failed": 1,
                                   "skipped": 1}


def test_a_host_with_docker_installed_but_not_running_is_skipped():
    """It otherwise fails deep inside testcontainers with an error that never
    mentions the daemon."""
    plan = plan_with()
    executor = ready(**{"docker info": (False, "Cannot connect to the daemon")})

    result = regression.suite_for("ace").run(executor, plan, plan.node("n1"),
                                             "main")

    assert result.status == regression.SKIPPED
    assert "Docker daemon" in result.detail


def test_a_host_without_go_is_skipped_naming_go():
    plan = plan_with()
    executor = ready(**{"command -v go": (False, "")})

    result = regression.suite_for("ace").run(executor, plan, plan.node("n1"),
                                             "main")

    assert result.status == regression.SKIPPED
    assert "go" in result.detail


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def results(*specs):
    return [regression.SuiteResult(component=name, status=status, ref="main",
                                   tests=tests or {},
                                   targets_cluster=targets)
            for name, status, tests, targets in specs]


def test_the_report_lists_every_suite_with_its_ref_and_verdict():
    text = "\n".join(health_check.summary_lines(results(
        ("spock", regression.PASSED, {"total": 23, "passed": 23, "failed": 0}, True),
        ("lolor", regression.SKIPPED, {}, True),
    )))

    assert "COMPONENT" in text and "REF" in text
    assert "23/23 passed" in text
    assert "SKIPPED" in text


def test_the_report_says_when_a_suite_tested_something_else():
    """A green ACE row next to green spock rows would otherwise read as
    evidence about the cluster."""
    text = "\n".join(health_check.summary_lines(results(
        ("ace", regression.PASSED, {"total": 5, "passed": 5, "failed": 0}, False),
    )))

    assert "not against this cluster" in text


def test_a_skipped_suite_is_not_claimed_to_have_tested_anything():
    text = "\n".join(health_check.summary_lines(results(
        ("ace", regression.SKIPPED, {}, False),
    )))

    assert "not against this cluster" not in text


# ---------------------------------------------------------------------------
# making a skip actionable
# ---------------------------------------------------------------------------


def test_a_missing_pgxs_names_the_package_to_install():
    """"the -devel package" leaves the reader to work out which one, and the
    answer differs per family and per major."""
    plan = plan_with()
    executor = FakeExecutor(replies={"pg_config --pgxs": (False, "")})

    result = regression.suite_for("spock").run(executor, plan, plan.node("n1"),
                                               "v5_STABLE")

    assert "pgedge-postgresql17-devel" in result.detail
    assert "--install-requirements" in result.detail


def test_the_debian_package_is_named_on_a_debian_host():
    plan = plan_with()
    plan.hosts[0].family = "deb"
    executor = FakeExecutor(replies={"pg_config --pgxs": (False, "")})

    result = regression.suite_for("spock").run(executor, plan, plan.node("n1"),
                                               "v5_STABLE")

    assert "pgedge-postgresql-17-dev" in result.detail


def test_install_requirements_installs_it_and_runs_the_suite():
    plan = plan_with()
    calls = {"probes": 0}

    class Executor(FakeExecutor):
        def try_run(self, command, **kwargs):
            if "pg_config --pgxs" in command:
                calls["probes"] += 1
                # Absent until the package is installed.
                if not any("devel" in c for c in self.commands):
                    self.commands.append(command)
                    return False, ""
                self.commands.append(command)
                return True, PGXS
            return super().try_run(command, **kwargs)

    executor = Executor(replies={
        f"test -f {PGXS}": (True, ""),
        "make installcheck": (True, "All 23 tests passed."),
    })

    result = regression.suite_for("spock").run(
        executor, plan, plan.node("n1"), "v5_STABLE",
        install_requirements=True, run_logger=RecordingLogger(),
    )

    assert any("pgedge-postgresql17-devel" in c for c in executor.commands)
    assert result.status == regression.PASSED
    assert calls["probes"] == 2, "probe, install, probe again"


def test_go_and_docker_are_not_installed_behind_the_operators_back():
    """A toolchain and a container runtime are a bigger decision than a
    -devel package, and not one a health check should take."""
    plan = plan_with()
    executor = ready(**{"command -v go": (False, "")})

    result = regression.suite_for("ace").run(executor, plan, plan.node("n1"),
                                             "main", install_requirements=True)

    assert result.status == regression.SKIPPED
    assert not any("install" in c and "go" in c for c in executor.commands)
    assert "--install-requirements" not in result.detail


# ---------------------------------------------------------------------------
# a run where nothing ran
# ---------------------------------------------------------------------------


def test_a_run_where_every_suite_skipped_does_not_claim_success():
    """Nothing failing is not the same as something passing."""
    text = "\n".join(health_check.summary_lines(results(
        ("spock", regression.SKIPPED, {}, True),
        ("ace", regression.SKIPPED, {}, False),
    )))

    assert "nothing was tested" in text


def test_a_run_with_one_pass_does_not_say_nothing_was_tested():
    text = "\n".join(health_check.summary_lines(results(
        ("spock", regression.PASSED, {"total": 1, "passed": 1, "failed": 0}, True),
        ("ace", regression.SKIPPED, {}, False),
    )))

    assert "nothing was tested" not in text
