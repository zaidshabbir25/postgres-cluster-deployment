#!/usr/bin/env python3
"""The add-node web form: what it offers, what it refuses, what it starts.

The form holds no rules of its own — it asks `/api/add-node/preview`, which
calls the same functions the CLI does. These tests cover the seam: that the
options are filtered to what this cluster permits, that a refusal reaches the
browser as a refusal, and that a submitted job runs once and can be followed.
No host is contacted and the mirror is never fetched.
"""

import time

import pytest

flask = pytest.importorskip("flask", reason="the dashboard needs Flask")

from aspects import state  # noqa: E402
from aspects.cluster_model import Host  # noqa: E402
from dashboard import app as dashboard, node_api  # noqa: E402
from deployment import add_node, add_standby, topology  # noqa: E402


SOURCE_INDEX = " ".join(
    f'<a href="v{version}/">v{version}/</a>'
    for version in ("16.9", "17.11", "18.6", "19beta1", "19beta2", "19beta3")
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """The version list comes from a fixed index, never from the network."""
    monkeypatch.setattr(node_api, "_source_index", lambda: SOURCE_INDEX)


@pytest.fixture
def cluster(state_dir, tmp_path, monkeypatch):
    """A saved two-node source-built cluster, with one spare inventory host."""
    hosts = [Host(name="host-a", address="10.0.1.11"),
             Host(name="host-b", address="10.0.1.12")]
    plan, _ = topology.plan_cluster(
        hosts, "pgedge", node_count=2, standby_of=["n1"], deploy_mode="source",
        pg_major="17", pg_version="17.11", spock_major="50",
        source_build={"spock_branch": "v5_STABLE"},
    )
    for host in plan.hosts:
        host.family = "rhel"
        host.advertise_address = host.address
    for node in plan.nodes:
        node.family = "rhel"
        node.pg_version = "17.11"
    state.save(plan)

    inventory_file = tmp_path / "inventory.json"
    inventory_file.write_text(
        '{"hosts": ['
        '{"name": "host-a", "host": "10.0.1.11", "enabled": true},'
        '{"name": "host-c", "host": "10.0.1.13", "username": "rocky", "enabled": true}'
        ']}', encoding="utf-8")
    return plan, str(inventory_file)


@pytest.fixture
def client(cluster):
    _, inventory_path = cluster
    app = dashboard.create_app(changes_allowed=True, inventory_path=inventory_path)
    return app.test_client()


@pytest.fixture
def readonly_client(cluster):
    _, inventory_path = cluster
    app = dashboard.create_app(changes_allowed=False, inventory_path=inventory_path)
    return app.test_client()


def preview(client, **form):
    return client.post("/api/add-node/preview", json=form).get_json()


# ---------------------------------------------------------------------------
# the page and its options
# ---------------------------------------------------------------------------


def test_the_page_renders(client):
    assert client.get("/add-node").status_code == 200


def test_options_describe_the_cluster(client):
    data = client.get("/api/add-node/options").get_json()

    assert data["cluster"]["name"] == "pgedge"
    assert data["cluster"]["deploy_mode"] == "source"
    assert data["cluster"]["pg_version"] == "17.11"
    assert data["cluster"]["spock_branch"] == "v5_STABLE"
    assert data["suggested_name"] == "n3"


def test_only_the_clusters_major_and_newer_are_offered(client):
    data = client.get("/api/add-node/options").get_json()

    assert data["pg_majors"] == ["17", "18", "19"]      # not 16
    assert data["pg_versions"]["19"] == ["19beta1", "19beta2", "19beta3"]


def test_only_the_clusters_spock_and_newer_are_offered(client):
    data = client.get("/api/add-node/options").get_json()

    assert data["spock_majors"] == ["50", "60"]
    assert data["spock_branches"] == {"50": "v5_STABLE", "60": "main"}


def test_hosts_include_the_cluster_and_the_spare_inventory(client):
    hosts = {h["name"]: h for h in client.get("/api/add-node/options").get_json()["hosts"]}

    assert hosts["host-a"]["in_cluster"] is True
    assert hosts["host-a"]["nodes"] == ["n1"]
    assert hosts["host-c"]["in_cluster"] is False      # offered, not yet joined


def test_a_package_cluster_is_not_offered_source_versions(state_dir, tmp_path):
    plan, _ = topology.plan_cluster([Host(name="a", address="10.0.1.11")],
                                    "pkg", node_count=1, deploy_mode="packages",
                                    pg_version="17.11")
    plan.hosts[0].family = "rhel"
    state.save(plan)
    client = dashboard.create_app(changes_allowed=True).test_client()

    data = client.get("/api/add-node/options?cluster=pkg").get_json()

    assert data["pg_versions"] == {}
    assert data["cluster"]["deploy_mode"] == "packages"


# ---------------------------------------------------------------------------
# preview — the same rules the CLI enforces
# ---------------------------------------------------------------------------


def test_a_default_spock_node_lands_on_a_free_port(client):
    data = preview(client, role="leader", host="host-a", source="n1")

    assert data["ok"] is True
    node = data["node"]
    assert (node["name"], node["pg_port"], node["restapi_port"]) == ("n3", 5433, 8009)
    assert node["scope"] == "pgedge-n3"
    assert node["bin_dir"] == "/opt/pgedge/pg17/bin"
    assert node["builds_from_source"] is True


def test_a_newer_major_changes_the_binaries_and_the_branch(client):
    data = preview(client, role="leader", host="host-b", pg_version="19beta3",
                   spock_major="60")

    assert data["ok"] is True
    assert data["node"]["bin_dir"] == "/opt/pgedge/pg19/bin"
    assert data["node"]["spock_branch"] == "main"
    assert any("mixed-version add" in w for w in data["warnings"])


def test_an_older_postgres_is_refused(client):
    data = preview(client, role="leader", host="host-a", pg_version="16.9")

    assert data["ok"] is False
    assert any("older than the cluster" in e for e in data["errors"])


def test_an_older_spock_is_refused(state_dir):
    plan, _ = topology.plan_cluster([Host(name="a", address="10.0.1.11")],
                                    "six", node_count=1, spock_major="60",
                                    pg_version="17.11")
    plan.hosts[0].family = "rhel"
    state.save(plan)
    client = dashboard.create_app(changes_allowed=True).test_client()

    data = client.post("/api/add-node/preview",
                       json={"cluster": "six", "role": "leader", "host": "a",
                             "spock_major": "50"}).get_json()

    assert data["ok"] is False
    assert any("older Spock" in e for e in data["errors"])


@pytest.mark.parametrize("name, reason", [
    ("n1", "already exists"),
    ("3bad", "must start with a letter"),
    ("bad-name", "must start with a letter"),
])
def test_bad_names_are_refused(client, name, reason):
    data = preview(client, role="leader", host="host-a", name=name)

    assert data["ok"] is False
    assert any(reason in error for error in data["errors"])


def test_a_standby_is_named_after_its_leader(client):
    """n1 already has n1s1, so the next one is n1s2."""
    first = preview(client, role="standby", host="host-b", leader="n2")
    second = preview(client, role="standby", host="host-b", leader="n1")

    assert first["node"]["name"] == "n2s1"
    assert first["node"]["scope"] == "pgedge-n2"
    assert second["node"]["name"] == "n1s2"


def test_a_standby_needs_a_leader(client):
    data = preview(client, role="standby", host="host-a")

    assert data["ok"] is False
    assert any("choose its leader" in e for e in data["errors"])


def test_a_standby_takes_no_versions(client):
    data = preview(client, role="standby", host="host-a", leader="n2",
                   pg_version="18.6")

    assert data["ok"] is False
    assert any("byte-for-byte copy" in e for e in data["errors"])


def test_the_replication_mode_belongs_to_a_standby(client):
    data = preview(client, role="leader", host="host-a", sync_mode="sync")

    assert data["ok"] is False
    assert any("applies to a standby" in e for e in data["errors"])


def test_a_host_outside_the_cluster_is_flagged_as_new(client):
    data = preview(client, role="leader", host="host-c")

    assert data["ok"] is True
    assert data["node"]["host_is_new"] is True
    assert any("not part of this cluster yet" in w for w in data["warnings"])


# ---------------------------------------------------------------------------
# submitting
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_add(monkeypatch):
    """Stand in for the real add: two steps, then whatever outcome is asked."""
    calls = []

    def make(outcome="succeeded"):
        def fake(**kwargs):
            calls.append(kwargs)
            logger = kwargs["run_logger"]
            for step in ("Prepare host", "Bootstrap with Patroni"):
                logger.step_start(step, "")
                logger.step_end("passed", f"{step} done")
            return {"outcome": outcome, "node": kwargs.get("node_name"),
                    "standby": "n2s1", "warnings": [], "failure": "boom",
                    "log_dir": str(logger.root)}
        return fake

    monkeypatch.setattr(add_node, "add", make())
    monkeypatch.setattr(add_standby, "add", make())
    return calls


def wait_for(client, job_id, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_a_submitted_node_runs_and_can_be_followed(client, fake_add):
    response = client.post("/api/add-node", json={
        "role": "leader", "host": "host-a", "name": "n3", "source": "n1",
        "pg_version": "18.6", "spock_major": "50",
    })

    assert response.status_code == 202
    job = wait_for(client, response.get_json()["id"])
    assert job["status"] == "succeeded"
    assert [step["name"] for step in job["steps"]] == [
        "Prepare host", "Bootstrap with Patroni"]
    assert fake_add[0]["node_name"] == "n3"
    assert fake_add[0]["pg_version"] == "18.6"


def test_a_standby_goes_to_the_standby_path(client, fake_add):
    response = client.post("/api/add-node", json={
        "role": "standby", "host": "host-b", "leader": "n2",
        "sync_mode": "sync", "sync_count": 1, "sync_strict": True,
    })

    assert response.status_code == 202
    wait_for(client, response.get_json()["id"])
    assert fake_add[0]["leader_name"] == "n2"
    assert fake_add[0]["synchronous_mode"] == "sync"
    assert fake_add[0]["synchronous_mode_strict"] is True


def test_a_refused_form_never_starts_a_job(client, fake_add):
    response = client.post("/api/add-node", json={
        "role": "leader", "host": "host-a", "pg_version": "16.9"})

    assert response.status_code == 400
    assert "older than the cluster" in response.get_json()["error"]
    assert fake_add == []


def test_only_one_add_runs_at_a_time(client, monkeypatch):
    """Two adds would race on the same state file and on each other's ports."""
    def slow(**kwargs):
        time.sleep(0.4)
        return {"outcome": "succeeded", "node": "n3", "warnings": []}

    monkeypatch.setattr(add_node, "add", slow)
    first = client.post("/api/add-node", json={"role": "leader", "host": "host-a"})
    second = client.post("/api/add-node", json={"role": "leader", "host": "host-b"})

    assert first.status_code == 202
    assert second.status_code == 409
    assert "still running" in second.get_json()["error"]


def test_a_failed_add_is_reported_as_failed(client, monkeypatch):
    monkeypatch.setattr(add_node, "add", lambda **kwargs: {
        "outcome": "failed", "failure": "the host refused the connection"})

    response = client.post("/api/add-node", json={"role": "leader", "host": "host-a"})
    job = wait_for(client, response.get_json()["id"])

    assert job["status"] == "failed"
    assert "refused the connection" in job["error"]


def test_an_exception_does_not_lose_the_job(client, monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("ssh died")

    monkeypatch.setattr(add_node, "add", explode)
    response = client.post("/api/add-node", json={"role": "leader", "host": "host-a"})
    job = wait_for(client, response.get_json()["id"])

    assert job["status"] == "failed"
    assert "ssh died" in job["error"]


def test_an_unknown_job_is_a_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


# ---------------------------------------------------------------------------
# the read-only default
# ---------------------------------------------------------------------------


def test_a_read_only_dashboard_refuses_to_add(readonly_client, fake_add):
    response = readonly_client.post("/api/add-node",
                                    json={"role": "leader", "host": "host-a"})

    assert response.status_code == 403
    assert "--allow-changes" in response.get_json()["error"]
    assert fake_add == []


def test_a_read_only_dashboard_still_previews(readonly_client):
    data = preview(readonly_client, role="leader", host="host-a")

    assert data["ok"] is True
    assert data["node"]["name"] == "n3"


def test_changes_allowed_is_reported_to_the_page(client, readonly_client):
    assert client.get("/api/add-node/options").get_json()["changes_allowed"] is True
    assert readonly_client.get("/api/add-node/options").get_json()["changes_allowed"] is False


def test_allow_changes_refuses_a_public_bind(capsys):
    """No authentication: a public bind must not accept cluster changes."""
    code = dashboard.main(["--host", "0.0.0.0", "--allow-changes"])

    assert code == 2
    assert "loopback" in capsys.readouterr().err
