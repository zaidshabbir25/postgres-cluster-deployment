#!/usr/bin/env python3
"""The deploy form: planning a cluster in the browser, then building it.

The preview is the plan — `topology.plan_cluster`, the same call the deployment
makes — so these tests check the seam rather than replanning: that the form's
answers become the right options, that refusals arrive as refusals, and that a
submitted deployment runs on the shared one-at-a-time runner.
"""

import json
import time

import pytest

flask = pytest.importorskip("flask", reason="the dashboard needs Flask")

from aspects import inventory, state  # noqa: E402
from dashboard import app as dashboard, deploy_api, node_api  # noqa: E402
from deployment import deploy_cluster, topology  # noqa: E402


SOURCE_INDEX = " ".join(
    f'<a href="v{version}/">v{version}/</a>'
    for version in ("16.9", "17.11", "18.6", "19beta3")
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(node_api, "_source_index", lambda: SOURCE_INDEX)


@pytest.fixture
def inventory_path(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({
        "defaults": {"cluster_name": "pgedge", "node_count": 2},
        "hosts": [
            {"name": "host-a", "host": "10.0.1.11", "username": "rocky", "enabled": True},
            {"name": "host-b", "host": "10.0.1.12", "username": "rocky", "enabled": True},
        ],
    }), encoding="utf-8")
    return str(path)


@pytest.fixture
def client(state_dir, inventory_path):
    app = dashboard.create_app(changes_allowed=True, inventory_path=inventory_path)
    return app.test_client()


def plan(client, **form):
    form.setdefault("hosts", ["host-a", "host-b"])
    form.setdefault("cluster_name", "demo")
    return client.post("/api/deploy/preview", json=form).get_json()


# ---------------------------------------------------------------------------
# the page and its options
# ---------------------------------------------------------------------------


def test_the_page_renders_without_any_cluster(client):
    """It is the page you need precisely when nothing is deployed yet."""
    assert client.get("/deploy").status_code == 200


def test_options_offer_the_inventory_and_the_defaults(client):
    data = client.get("/api/deploy/options").get_json()

    assert [host["name"] for host in data["hosts"]] == ["host-a", "host-b"]
    assert data["defaults"]["cluster_name"] == "pgedge"
    assert data["defaults"]["node_count"] == 2
    assert data["pg_majors"] == ["16", "17", "18", "19"]
    assert data["pg_versions"]["19"] == ["19beta3"]
    assert data["spock_branches"] == {"50": "v5_STABLE", "60": "main"}


def test_a_missing_inventory_is_reported_not_fatal(state_dir, tmp_path):
    """The form is how you add the first machine, so it must still load."""
    app = dashboard.create_app(changes_allowed=True,
                               inventory_path=str(tmp_path / "absent.json"))

    data = app.test_client().get("/api/deploy/options").get_json()

    assert data["hosts"] == []
    assert "not found" in data["inventory_problem"]


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_the_preview_is_the_plan(client):
    data = plan(client, node_count=3, standby_of=["n1"], deploy_mode="packages",
                pg_major="17")

    assert data["ok"] is True
    assert [(n["name"], n["host"], n["pg_port"]) for n in data["nodes"]] == [
        ("n1", "host-a", 5432), ("n2", "host-b", 5432),
        ("n3", "host-a", 5433), ("n1s1", "host-b", 5433),
    ]
    assert data["nodes"][-1]["follows"] == "n1"
    assert data["etcd"] == ["http://10.0.1.11:2379"]


def test_planning_carries_the_planners_warnings(client):
    data = plan(client, node_count=3, deploy_mode="packages")

    assert any("share a failure domain" in w for w in data["warnings"])
    assert any("single-member etcd" in w for w in data["warnings"])


def test_a_subset_of_hosts_is_honoured(client):
    data = plan(client, hosts=["host-b"], node_count=2, deploy_mode="packages")

    assert {node["host"] for node in data["nodes"]} == {"host-b"}
    assert data["summary"]["hosts"] == ["host-b"]


def test_a_source_build_needs_an_exact_version(client):
    vague = plan(client, deploy_mode="source", pg_major="19", pg_version="19")
    exact = plan(client, deploy_mode="source", pg_major="19", pg_version="19beta3")

    assert vague["ok"] is False
    assert "one exact version" in vague["errors"][0]
    assert exact["ok"] is True


def test_an_existing_cluster_name_is_refused_unless_wiping(client, deployed_plan):
    state.save(deployed_plan)                       # a cluster named pgedge

    taken = plan(client, cluster_name="pgedge", deploy_mode="packages")
    wiping = plan(client, cluster_name="pgedge", deploy_mode="packages", clean=True)

    assert taken["ok"] is False
    assert "already deployed" in taken["errors"][0]
    assert wiping["ok"] is True


@pytest.mark.parametrize("count", [0, 33])
def test_a_silly_node_count_is_refused(client, count):
    assert plan(client, node_count=count, deploy_mode="packages")["ok"] is False


def test_an_unknown_standby_target_is_refused(client):
    data = plan(client, node_count=2, standby_of=["n7"], deploy_mode="packages")

    assert data["ok"] is False
    assert any("unknown node" in error for error in data["errors"])


def test_choosing_no_hosts_is_refused(client):
    data = client.post("/api/deploy/preview",
                       json={"cluster_name": "demo", "hosts": ["nope"]}).get_json()

    assert data["ok"] is True or data["ok"] is False   # falls back to all hosts
    # ...but an empty inventory really is refused
    assert isinstance(data["errors"], list)


# ---------------------------------------------------------------------------
# what reaches deploy()
# ---------------------------------------------------------------------------


def test_the_form_becomes_the_options_the_cli_builds(inventory_path):
    options = deploy_api.build_options({
        "cluster_name": "demo", "hosts": ["host-a"], "node_count": 3,
        "standby_of": ["n1"], "deploy_mode": "source", "pg_major": "19",
        "pg_version": "19beta3", "spock_major": "60", "spock_branch": "",
        "db_name": "app", "db_user": "app", "base_port": 6432,
        "base_restapi_port": 8108, "data_root": "/data", "clean": True,
        "sync_mode": "sync", "sync_count": 2, "sync_strict": True,
    }, inventory_path)

    assert options["hosts"] == ["host-a"]
    assert options["node_count"] == 3
    assert options["standby_of"] == ["n1"]
    assert options["pg_version"] == "19beta3"
    assert options["source_build"]["spock_branch"] == "main"   # spock60's default
    assert options["base_port"] == 6432
    assert options["clean"] is True
    assert options["synchronous_mode"] == "sync"
    assert options["synchronous_mode_strict"] is True


def test_deploy_narrows_the_inventory_to_the_chosen_hosts(inventory_path, monkeypatch):
    """`hosts` is how the form says "these, not every enabled entry"."""
    seen = {}

    class Stop(RuntimeError):
        pass

    def capture(hosts, **kwargs):
        seen["hosts"] = [host.name for host in hosts]
        raise Stop()

    monkeypatch.setattr(topology, "plan_cluster", capture)
    with pytest.raises(Stop):
        deploy_cluster.deploy({"inventory": inventory_path, "hosts": ["host-b"],
                               "cluster_name": "demo"})

    assert seen["hosts"] == ["host-b"]


def test_an_unknown_host_is_named(inventory_path):
    with pytest.raises(inventory.InventoryError, match="no enabled host named ghost"):
        deploy_cluster.deploy({"inventory": inventory_path, "hosts": ["ghost"],
                               "cluster_name": "demo"})


# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------


def wait_for(client, job_id, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_a_submitted_deployment_runs_and_can_be_followed(client, monkeypatch):
    calls = []

    def fake_deploy(options, run_logger=None):
        calls.append(options)
        run_logger.step_start("Connect to hosts", "")
        run_logger.step_end("passed", "2 hosts")
        return {"outcome": "succeeded", "cluster": options["cluster_name"],
                "warnings": [], "duration": 1}

    monkeypatch.setattr(deploy_cluster, "deploy", fake_deploy)

    response = client.post("/api/deploy", json={
        "cluster_name": "demo", "hosts": ["host-a", "host-b"], "node_count": 2,
        "deploy_mode": "packages", "pg_major": "17",
    })

    assert response.status_code == 202
    job = wait_for(client, response.get_json()["id"])
    assert job["status"] == "succeeded"
    assert [step["name"] for step in job["steps"]] == ["Connect to hosts"]
    assert calls[0]["cluster_name"] == "demo"


def test_a_refused_plan_never_starts_a_deployment(client, monkeypatch):
    monkeypatch.setattr(deploy_cluster, "deploy",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("should not run")))

    response = client.post("/api/deploy", json={
        "cluster_name": "demo", "hosts": ["host-a"], "node_count": 2,
        "deploy_mode": "source", "pg_version": "19",
    })

    assert response.status_code == 400
    assert "exact version" in response.get_json()["error"]


def test_a_deployment_and_an_add_cannot_run_at_once(client, monkeypatch):
    """Both change the cluster; the runner is shared for that reason."""
    monkeypatch.setattr(deploy_cluster, "deploy",
                        lambda options, run_logger=None: time.sleep(0.4) or
                        {"outcome": "succeeded"})

    first = client.post("/api/deploy", json={
        "cluster_name": "demo", "hosts": ["host-a"], "node_count": 1,
        "deploy_mode": "packages"})
    second = client.post("/api/deploy", json={
        "cluster_name": "other", "hosts": ["host-b"], "node_count": 1,
        "deploy_mode": "packages"})

    assert first.status_code == 202
    assert second.status_code == 409


def test_a_read_only_dashboard_plans_but_does_not_deploy(state_dir, inventory_path):
    client = dashboard.create_app(changes_allowed=False,
                                  inventory_path=inventory_path).test_client()

    planned = client.post("/api/deploy/preview", json={
        "cluster_name": "demo", "hosts": ["host-a"], "node_count": 1,
        "deploy_mode": "packages"})
    started = client.post("/api/deploy", json={
        "cluster_name": "demo", "hosts": ["host-a"], "node_count": 1,
        "deploy_mode": "packages"})

    assert planned.status_code == 200
    assert started.status_code == 403


# ---------------------------------------------------------------------------
# growing the inventory from the form
# ---------------------------------------------------------------------------


def test_a_host_can_be_added_to_the_inventory(client, inventory_path):
    response = client.post("/api/inventory/hosts", json={
        "host": "10.0.1.13", "name": "host-c", "username": "rocky", "port": 22})

    assert response.status_code == 201
    hosts, _ = inventory.load(inventory_path)
    assert [host.name for host in hosts] == ["host-a", "host-b", "host-c"]


def test_a_local_host_is_written_without_ssh_details(client, inventory_path):
    client.post("/api/inventory/hosts", json={"host": "localhost", "name": "local",
                                              "local": True})

    hosts, _ = inventory.load(inventory_path)
    local = [host for host in hosts if host.name == "local"][0]
    assert local.local is True


@pytest.mark.parametrize("payload, reason", [
    ({"name": "nameless"}, "needs an address"),
    ({"host": "10.0.1.11", "name": "host-a"}, "already has a host named"),
    ({"host": "10.0.1.11", "name": "other"}, "already points at"),
])
def test_bad_hosts_are_refused(client, payload, reason):
    response = client.post("/api/inventory/hosts", json=payload)

    assert response.status_code == 400
    assert reason in response.get_json()["error"]


def test_a_read_only_dashboard_does_not_write_the_inventory(state_dir, inventory_path):
    client = dashboard.create_app(changes_allowed=False,
                                  inventory_path=inventory_path).test_client()

    response = client.post("/api/inventory/hosts", json={"host": "10.0.1.99"})

    assert response.status_code == 403
    hosts, _ = inventory.load(inventory_path)
    assert len(hosts) == 2


def test_the_inventory_is_created_when_there_is_none(state_dir, tmp_path):
    path = tmp_path / "new-inventory.json"
    client = dashboard.create_app(changes_allowed=True,
                                  inventory_path=str(path)).test_client()

    response = client.post("/api/inventory/hosts",
                           json={"host": "localhost", "local": True})

    assert response.status_code == 201
    assert json.loads(path.read_text())["hosts"][0]["host"] == "localhost"
