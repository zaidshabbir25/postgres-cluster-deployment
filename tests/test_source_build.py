#!/usr/bin/env python3
"""Building from source: prefixes, permissions, and which Spock branch."""

import pytest

from aspects import package_management, source_build as sb
from aspects.cluster_model import ClusterPlan, Host

from conftest import FakeExecutor, RecordingLogger


@pytest.fixture
def source_plan():
    return ClusterPlan(cluster_name="pgedge", pg_major="17", pg_version="17.11",
                       spock_major="50", deploy_mode="source",
                       source_build={"pg_version": "17.11"})


@pytest.fixture
def host():
    probed = Host(name="local", address="localhost", local=True)
    probed.family = "rhel"
    probed.platform = {"arch": "x86_64"}
    return probed


@pytest.fixture(autouse=True)
def stub_slow_steps(monkeypatch):
    """Everything that would fetch or compile, replaced by a note."""
    monkeypatch.setattr(package_management, "install",
                        lambda *a, **k: (["dep"], []))
    monkeypatch.setattr(sb, "install_build_dependencies",
                        lambda e, f, node=None: (["dep"], "20 build packages installed"))
    monkeypatch.setattr(sb, "ensure_postgres_user", lambda *a, **k: None)
    monkeypatch.setattr(sb, "fetch_postgres_source",
                        lambda e, v, node=None: f"/opt/pgedge/build/postgresql-{v}")
    monkeypatch.setattr(sb, "fetch_spock_source",
                        lambda e, b="main", node=None: ("/opt/pgedge/build/spock", "abc123"))
    monkeypatch.setattr(sb, "apply_spock_patches",
                        lambda *a, **k: (3, ["p1", "p2", "p3"]))


# ---------------------------------------------------------------------------
# paths and branches
# ---------------------------------------------------------------------------


def test_install_dir_is_per_major():
    assert sb.install_dir("17") == "/opt/pgedge/pg17"
    assert sb.bin_dir("18") == "/opt/pgedge/pg18/bin"


@pytest.mark.parametrize("major, branch", [
    ("50", "v5_STABLE"),
    ("60", "main"),
    (50, "v5_STABLE"),
    ("70", "main"),        # unknown majors fall back rather than fail
])
def test_default_spock_branch(major, branch):
    assert sb.default_spock_branch(major) == branch


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------


def test_make_reachable_opens_the_tree_and_its_parent():
    """Installed by root; run by postgres. A 0077 umask breaks that."""
    executor = FakeExecutor()

    sb.make_reachable(executor, "/opt/pgedge/pg17")

    assert executor.ran("chmod a+rx /opt/pgedge")
    assert executor.ran("chmod -R a+rX /opt/pgedge/pg17")
    assert executor.ran("find /opt/pgedge/pg17 -type d -exec chmod a+rx")


def test_make_reachable_is_fatal_when_a_chmod_fails():
    """A silent chmod failure showed up six steps later as a dead node."""
    executor = FakeExecutor({"chmod -R": (False, "operation not permitted")})

    with pytest.raises(Exception):
        sb.make_reachable(executor, "/opt/pgedge/pg17")


def test_make_venv_reachable_follows_an_interpreter_inside_the_tree():
    executor = FakeExecutor({"readlink -f": (True, "/opt/pgedge/pg17/bin/python3")})

    sb.make_venv_reachable(executor, sb.PATRONI_VENV)

    assert executor.ran("chmod a+rx /opt/pgedge/pg17/bin/python3")


def test_make_venv_reachable_leaves_the_system_interpreter_alone():
    executor = FakeExecutor({"readlink -f": (True, "/usr/bin/python3.9")})

    sb.make_venv_reachable(executor, sb.PATRONI_VENV)

    assert not executor.ran("chmod a+rx /usr/bin/python3.9")


# ---------------------------------------------------------------------------
# the Patroni venv
# ---------------------------------------------------------------------------


def test_venv_is_built_from_a_system_interpreter():
    """Never plain `python3`: a local run inherits this tool's own venv."""
    executor = FakeExecutor()

    assert sb.system_python(executor) == "/usr/bin/python3"


def test_system_python_falls_back_to_the_path():
    executor = FakeExecutor(files=())

    assert sb.system_python(executor) == "/usr/bin/python3"   # via which()


def test_a_venv_wired_to_a_foreign_interpreter_is_rebuilt():
    """The bug: bin/python3 -> /root/<checkout>/venv/bin/python3, unreadable."""
    executor = FakeExecutor({
        "readlink /opt/pgedge/patroni-venv/bin/python3":
            (True, "/root/postgres-cluster-deployment/venv/bin/python3"),
    })

    sb.install_patroni_from_pip(executor, "rhel")

    assert executor.ran("rm -rf /opt/pgedge/patroni-venv")
    assert executor.ran("/usr/bin/python3 -m venv /opt/pgedge/patroni-venv")


def test_a_venv_on_the_system_interpreter_is_reused():
    executor = FakeExecutor({
        "readlink /opt/pgedge/patroni-venv/bin/python3": (True, "/usr/bin/python3"),
    })

    sb.install_patroni_from_pip(executor, "rhel")

    assert not executor.ran("rm -rf /opt/pgedge/patroni-venv")


def test_patroni_is_symlinked_and_made_reachable():
    executor = FakeExecutor()

    sb.install_patroni_from_pip(executor, "rhel")

    assert executor.ran("ln -sf /opt/pgedge/patroni-venv/bin/patroni /usr/local/bin/patroni")
    assert executor.ran("ln -sf /opt/pgedge/patroni-venv/bin/patronictl /usr/local/bin/patronictl")
    assert executor.ran("chmod -R a+rX /opt/pgedge/patroni-venv")


# ---------------------------------------------------------------------------
# building one major alongside another
# ---------------------------------------------------------------------------


def test_build_major_installs_into_its_own_prefix(source_plan, host):
    executor = FakeExecutor()
    logger = RecordingLogger()

    details = sb.build_major(executor, host, source_plan, "18.6", run_logger=logger)

    assert details["prefix"] == "/opt/pgedge/pg18"
    assert details["spock_branch"] == "v5_STABLE"      # the cluster's major
    assert executor.ran("--prefix=/opt/pgedge/pg18")
    assert logger.said("PostgreSQL 18.6 installed to /opt/pgedge/pg18")


def test_build_major_takes_the_branch_it_is_given(source_plan, host):
    details = sb.build_major(FakeExecutor(), host, source_plan, "18.6",
                             spock_branch="main")

    assert details["spock_branch"] == "main"


def test_build_major_registers_the_library_path_per_major(source_plan, host):
    executor = FakeExecutor()

    sb.build_major(executor, host, source_plan, "18.6")

    assert "/etc/ld.so.conf.d/pgedge-source-pg18.conf" in executor.written
    assert executor.written["/etc/ld.so.conf.d/pgedge-source-pg18.conf"] == \
        "/opt/pgedge/pg18/lib\n"


def test_build_major_leaves_the_clusters_own_paths_alone(source_plan, host):
    """PATH and the client symlinks belong to the cluster's major."""
    executor = FakeExecutor()

    sb.build_major(executor, host, source_plan, "18.6")

    assert "/etc/profile.d/pgedge-source.sh" not in executor.written
    assert not executor.ran("ln -sf /opt/pgedge/pg18/bin/psql")


def test_build_major_needs_an_exact_version(source_plan, host):
    with pytest.raises(ValueError, match="full PostgreSQL version"):
        sb.build_major(FakeExecutor(), host, source_plan, "18")


def test_rebuild_spock_only_touches_spock(source_plan, host):
    executor = FakeExecutor()
    logger = RecordingLogger()

    details = sb.rebuild_spock(executor, host, source_plan, "17", "main",
                               run_logger=logger)

    assert details["spock_branch"] == "main"
    assert executor.ran("USE_PGXS=1 make install")      # Spock, via PGXS
    assert not executor.ran("./configure")              # not PostgreSQL itself
    assert logger.said("Spock rebuilt from main")


def test_build_host_defaults_the_branch_from_the_spock_major(source_plan, host,
                                                             monkeypatch):
    captured = {}
    monkeypatch.setattr(sb, "fetch_spock_source",
                        lambda e, b="main", node=None: (captured.setdefault("branch", b),
                                                        "abc123"))
    monkeypatch.setattr(sb, "install_patroni_from_pip", lambda *a, **k: "patroni 4")
    monkeypatch.setattr(sb, "install_etcd_from_release", lambda *a, **k: "etcd 3.5")
    monkeypatch.setattr(sb, "register_paths", lambda *a, **k: "/opt/pgedge/pg17")

    sb.build_host(FakeExecutor(), host, source_plan)

    assert captured["branch"] == "v5_STABLE"
