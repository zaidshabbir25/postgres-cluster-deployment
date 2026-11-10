#!/usr/bin/env python3
"""Putting a different Spock build onto a running cluster.

The thing to get right is the order: build everywhere first, restart only when
every host built, update the extension only after the restart. A cluster left
half-built and restarted runs two Spocks at once, which is worse than the bug
being validated.
"""

import pytest

from aspects import source_build, state
from deployment import spock_rebuild

from conftest import FakeExecutor


# ---------------------------------------------------------------------------
# fetching any ref
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["pull/626/head", "v5_STABLE", "v5.0.11",
                                 "4a4dd44f6a"])
def test_any_ref_form_is_fetched_the_same_way(ref):
    """git clone --branch takes only branches and tags; a pull request and a
    bare commit need fetch + checkout FETCH_HEAD, so every form goes that way."""
    executor = FakeExecutor({"rev-parse": (True, "4a4dd44\n"),
                             "describe": (True, "v6.0.0-12-g4a4dd44\n")})

    path, revision, describe = source_build.fetch_spock_ref(executor, ref)

    assert executor.ran(f"fetch --depth 1 origin {ref}")
    assert executor.ran("checkout -q FETCH_HEAD")
    assert not executor.ran(f"--branch {ref}")
    assert revision == "4a4dd44" and describe.startswith("v6.0.0")
    assert path.endswith("/spock")


def test_the_checkout_starts_clean():
    """A leftover tree from the last validation must not leak objects into
    this one: a stale .o against different headers builds silently wrong."""
    executor = FakeExecutor()

    source_build.fetch_spock_ref(executor, "main")

    assert executor.commands.index(next(
        c for c in executor.commands if "rm -rf" in c)) < \
        executor.commands.index(next(
            c for c in executor.commands if "git clone" in c))


# ---------------------------------------------------------------------------
# building against the server that is there
# ---------------------------------------------------------------------------


def test_the_build_uses_the_node_pg_config_not_a_guessed_prefix():
    """A packaged cluster has pg_config in /usr/pgsql-17/bin; the source prefix
    build_spock assumes does not exist there."""
    executor = FakeExecutor({"--pkglibdir": (True, "/usr/pgsql-17/lib\n"),
                             "md5sum": (True, "abc123def456\n")})

    module, checksum = source_build.build_spock_against(
        executor, "/opt/pgedge/build/spock", "/usr/pgsql-17/bin")

    assert executor.ran("PATH=/usr/pgsql-17/bin:$PATH USE_PGXS=1 make")
    assert module == "/usr/pgsql-17/lib/spock.so"
    assert checksum == "abc123def456"


def test_a_build_that_produces_no_module_is_an_error():
    executor = FakeExecutor({"--pkglibdir": (True, "/usr/pgsql-17/lib\n"),
                             "test -f": (False, "")})

    with pytest.raises(RuntimeError, match="spock.so is missing"):
        source_build.build_spock_against(executor, "/b/spock", "/usr/pgsql-17/bin")


# ---------------------------------------------------------------------------
# the rebuild, end to end against fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def cluster(monkeypatch, deployed_plan):
    """A two-node packaged cluster, with everything remote scripted."""
    plan = deployed_plan
    plan.db_password = "secret"
    executors = {}
    saved = {}

    def fake_executor_for(plan_, host_name, cache, run_logger):
        if host_name not in executors:
            executors[host_name] = FakeExecutor({
                "spock.spock_version()": (True, "5.0.12\n"),
                "extversion FROM pg_extension": (True, "5.0.12\n"),
                "default_version": (True, "6.0.0\n"),
                "--pkglibdir": (True, "/usr/pgsql-17/lib\n"),
                "md5sum": (True, "feedfacecafe\n"),
                "rev-parse": (True, "4a4dd44\n"),
                "describe": (True, "v6.0.0-rc1\n"),
                "SELECT 1;": (True, "1\n"),
            })
        return executors[host_name]

    monkeypatch.setattr(spock_rebuild, "_executor_for", fake_executor_for)
    monkeypatch.setattr(state, "load", lambda *a, **k: (plan, {}))
    monkeypatch.setattr(state, "resolve_password", lambda plan, explicit=None: "secret")
    monkeypatch.setattr(state, "save",
                        lambda plan, extra=None: saved.update(extra or {}))
    monkeypatch.setattr(spock_rebuild.time, "sleep", lambda *_: None)
    # Patroni is not here; a restart "succeeds" and the node is "back".
    monkeypatch.setattr(spock_rebuild.cluster_services, "restart",
                        lambda *a, **k: (True, "restarted"))
    return plan, executors, saved


def test_every_host_builds_and_every_node_restarts(cluster):
    plan, executors, _ = cluster

    result = spock_rebuild.rebuild("pgedge", "pull/626/head")

    assert result["outcome"] in ("succeeded", "succeeded_with_warnings"), result
    for executor in executors.values():
        assert executor.ran("fetch --depth 1 origin pull/626/head")
        assert executor.ran("make install")
    assert set(result["after"]) == {n.name for n in plan.nodes}


def test_a_packaged_cluster_gets_the_devel_package_first(cluster):
    """PGXS needs pg_config's makefile, which the server package does not
    carry; without the -devel package the build fails on the first host."""
    _, executors, _ = cluster

    spock_rebuild.rebuild("pgedge", "main")

    assert any(e.ran("pgedge-postgresql17-devel") for e in executors.values())


def test_the_extension_is_updated_when_its_sql_moved(cluster):
    """The fakes say 5.0.12 is created and 6.0.0 is now available: that gap is
    exactly what ALTER EXTENSION UPDATE closes."""
    _, executors, _ = cluster

    spock_rebuild.rebuild("pgedge", "main")

    assert any(e.ran("ALTER EXTENSION spock UPDATE") for e in executors.values())


def test_the_previous_build_is_remembered_so_it_can_be_undone(cluster):
    _, _, saved = cluster

    result = spock_rebuild.rebuild("pgedge", "pull/626/head")

    record = saved["spock_rebuild"]
    assert record["ref"] == "pull/626/head"
    assert record["previous_ref"] == "package"      # it came from a package
    assert result["previous_ref"] == "package"


def test_a_failed_build_restarts_nothing(cluster, monkeypatch):
    """Half the hosts on the new module and half on the old is the one state
    worse than either; a build failure anywhere stops before any restart."""
    _, executors, saved = cluster
    monkeypatch.setattr(source_build, "build_spock_against",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("cc: error")))
    restarts = []
    monkeypatch.setattr(spock_rebuild.cluster_services, "restart",
                        lambda *a, **k: restarts.append(a) or (True, ""))

    result = spock_rebuild.rebuild("pgedge", "main")

    assert result["outcome"] == "failed"
    assert "nothing was restarted" in result["error"]
    assert restarts == []
    assert "spock_rebuild" not in saved


def test_no_restart_means_no_update_and_a_warning(cluster):
    """Updating the SQL while the old module is still loaded would call
    functions the library does not have yet."""
    _, executors, _ = cluster

    result = spock_rebuild.rebuild("pgedge", "main", restart=False)

    assert not any(e.ran("ALTER EXTENSION") for e in executors.values())
    assert any("not restarted" in w for w in result["warnings"])


def test_versions_reports_loaded_created_and_available_per_node(cluster):
    plan, _, _ = cluster

    rows = spock_rebuild.versions("pgedge")

    assert [r["node"] for r in rows] == [n.name for n in plan.nodes]
    assert rows[0]["loaded"] == "5.0.12"
    assert rows[0]["available"] == "6.0.0"
    assert rows[0]["module"] == "/usr/pgsql-17/lib/spock.so"
