#!/usr/bin/env python3
"""The add-node form's backend: what it may offer, and what it starts.

Every rule here is the one the CLI already enforces — `check_version`,
`check_spock`, `resolve_host`, `next_ports` — imported rather than restated, so
the form cannot drift from `./pg_deploy_cluster.sh --add-node`. The browser gets
a preview of exactly what would be built, and the same call refuses the same
things.

Adding a node takes minutes (an hour, for a source build that compiles a major),
so the POST starts a background job and returns its id. The page then polls for
the step list the RunLogger is already keeping.
"""

import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from flask import Blueprint, jsonify, render_template, request

from aspects import inventory, pg_server_management, source_build, state
from aspects.logging_setup import RunLogger, new_run_id
from deployment import add_node, add_standby

# How long a fetched list of published PostgreSQL versions stays fresh. The
# mirror changes a few times a year; re-reading it on every keystroke would be
# rude and slow.
VERSION_CACHE_SECONDS = 3600

SPOCK_MAJORS = ("50", "60")


# ---------------------------------------------------------------------------
# Published PostgreSQL versions
# ---------------------------------------------------------------------------

_versions_lock = threading.Lock()
_versions_cache = {"fetched": 0.0, "index": ""}


def _source_index():
    """The PostgreSQL source listing, cached. "" when unreachable."""
    with _versions_lock:
        fresh = time.time() - _versions_cache["fetched"] < VERSION_CACHE_SECONDS
        if fresh and _versions_cache["index"]:
            return _versions_cache["index"]
    try:
        with urlopen(source_build.PG_SOURCE_INDEX, timeout=15) as response:
            html = response.read().decode("utf-8", "replace")
    except Exception:
        html = ""
    with _versions_lock:
        if html:
            _versions_cache.update(fetched=time.time(), index=html)
        return html or _versions_cache["index"]


def published_versions(majors):
    """{major: [versions]} for the majors a new node may run."""
    index = _source_index()
    if not index:
        return {}
    return {
        major: source_build.parse_source_index(index, major)
        for major in majors
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


class Job:
    """One background add, and everything the page needs to follow it."""

    def __init__(self, kind, cluster, summary, params):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                     # "node" | "standby"
        self.cluster = cluster
        self.summary = summary
        self.params = params
        self.status = "running"              # running | succeeded | failed
        self.started_at = datetime.now(timezone.utc)
        self.finished_at = None
        self.result = None
        self.error = None
        self.logger = RunLogger(new_run_id(f"{cluster}-{kind}-web"))

    # --- what the browser polls -------------------------------------
    def view(self, log_lines=60):
        steps = [
            {
                "name": step.get("name"),
                "detail": step.get("detail", ""),
                "status": step.get("status", "running"),
                "message": step.get("message", ""),
                "duration": step.get("duration"),
            }
            for step in (self.logger.steps or [])
        ]
        elapsed = (self.finished_at or datetime.now(timezone.utc)) - self.started_at
        return {
            "id": self.id,
            "kind": self.kind,
            "cluster": self.cluster,
            "summary": self.summary,
            "status": self.status,
            "elapsed": round(elapsed.total_seconds(), 1),
            "steps": steps,
            "log": self.tail(log_lines),
            "log_dir": str(self.logger.root),
            "error": self.error,
            "warnings": (self.result or {}).get("warnings", []),
            "node": (self.result or {}).get("node")
                    or (self.result or {}).get("standby"),
        }

    def tail(self, lines=60):
        path = Path(self.logger.root) / "deploy.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])


class JobRunner:
    """Runs one add at a time, and remembers the recent ones.

    One at a time deliberately: two adds against a cluster would race on the
    same state file and on each other's ports.
    """

    def __init__(self, keep=20):
        self.keep = keep
        self._jobs = {}
        self._order = []
        self._lock = threading.Lock()
        self._running = None

    def running(self):
        with self._lock:
            job = self._jobs.get(self._running) if self._running else None
            return job if job and job.status == "running" else None

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self):
        with self._lock:
            return [self._jobs[job_id] for job_id in reversed(self._order)
                    if job_id in self._jobs]

    def start(self, kind, cluster, summary, params, call):
        busy = self.running()
        if busy is not None:
            raise RuntimeError(
                f"{busy.summary} is still running. One change at a time: two "
                f"adds would race on the same cluster state."
            )

        job = Job(kind, cluster, summary, params)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            for stale in self._order[:-self.keep]:
                self._jobs.pop(stale, None)
            self._order = self._order[-self.keep:]
            self._running = job.id

        def run():
            try:
                job.result = call(job.logger)
                job.status = ("succeeded"
                              if (job.result or {}).get("outcome") == "succeeded"
                              else "failed")
                job.error = (job.result or {}).get("failure")
            except Exception as exc:                       # noqa: BLE001
                job.status = "failed"
                job.error = str(exc)
            finally:
                job.finished_at = datetime.now(timezone.utc)

        threading.Thread(target=run, name=f"add-{job.id}", daemon=True).start()
        return job


# ---------------------------------------------------------------------------
# Reading the cluster
# ---------------------------------------------------------------------------


def cluster_options(cluster_name, inventory_path=None):
    """Everything the form needs to offer sensible choices."""
    plan, _ = state.load(cluster_name)
    cluster_major = pg_server_management.major_of(
        plan.pg_version or plan.pg_major) or plan.pg_major

    # A new node may match the cluster's major or take a newer one.
    majors = [m for m in ("16", "17", "18", "19")
              if int(m) >= int(cluster_major or 17)]
    versions = published_versions(majors) if plan.deploy_mode == "source" else {}

    in_cluster = {host.name for host in plan.hosts}
    spare = []
    try:
        hosts, _ = inventory.load(inventory_path)
        spare = [
            {"name": host.name, "address": host.address,
             "username": host.username, "in_cluster": False}
            for host in hosts if host.name not in in_cluster
        ]
    except Exception:
        spare = []

    return {
        "cluster": {
            "name": plan.cluster_name,
            "deploy_mode": plan.deploy_mode,
            "repo_channel": plan.repo_channel,
            "pg_version": plan.pg_version or plan.pg_major,
            "pg_major": cluster_major,
            "spock_major": str(plan.spock_major),
            "spock_branch": (plan.source_build or {}).get("spock_branch")
                            or source_build.default_spock_branch(plan.spock_major),
            "data_root": plan.data_root,
            "synchronous_mode": plan.synchronous_mode,
            "synchronous_node_count": plan.synchronous_node_count,
            "synchronous_mode_strict": plan.synchronous_mode_strict,
        },
        "suggested_name": add_node.next_node_name(plan),
        "nodes": [
            {"name": node.name, "role": node.role, "host": node.host,
             "address": node.address, "pg_port": node.pg_port,
             "scope": node.scope, "leader": node.leader,
             "pg_version": node.pg_version,
             "spock_major": node.spock_major or str(plan.spock_major)}
            for node in plan.nodes
        ],
        "hosts": [
            {"name": host.name, "address": host.address,
             "username": host.username, "family": host.family,
             "in_cluster": True,
             "nodes": [n.name for n in plan.nodes_on(host.name)]}
            for host in plan.hosts
        ] + spare,
        "pg_majors": majors,
        "pg_versions": versions,
        "spock_majors": [m for m in SPOCK_MAJORS
                         if int(m) >= int(plan.spock_major)],
        "spock_branches": {m: source_build.default_spock_branch(m)
                           for m in SPOCK_MAJORS},
        "sync_modes": ["async", "sync", "quorum"],
    }


def preview(cluster_name, form, inventory_path=None):
    """Resolve a form into the node it would build, or the reason it cannot.

    Returns {"ok": bool, "errors": [...], "warnings": [...], "node": {...}}.
    Nothing is contacted and nothing is changed: this is the plan only.
    """
    errors, warnings = [], []
    plan, _ = state.load(cluster_name)

    role = (form.get("role") or "leader").lower()
    leader_name = (form.get("leader") or "").strip()
    leader = next((n for n in plan.spock_nodes if n.name == leader_name), None)

    if role == "standby":
        # A standby is named after the node it follows — n1s1, n1s2 — so that
        # the name says what it is. There is nothing to ask.
        name = (add_standby.next_standby_name(plan, leader) if leader else "")
    else:
        name = (form.get("name") or "").strip() or add_node.next_node_name(plan)
        if not name.replace("_", "").isalnum() or not name[0].isalpha():
            errors.append(f"node name {name!r} must start with a letter and "
                          f"contain only letters, digits or underscores")
        if name in {node.name for node in plan.nodes}:
            errors.append(f"node {name!r} already exists in this cluster")

    # --- where it goes ----------------------------------------------
    host = None
    host_name = (form.get("host") or "").strip() or None
    try:
        host, is_new, note = add_node.resolve_host(plan, host_name, inventory_path)
        if note:
            warnings.append(note)
    except add_node.AddNodeError as exc:
        errors.append(str(exc))
        is_new = False

    # --- what it runs ------------------------------------------------
    pg_version = (form.get("pg_version") or "").strip()
    spock_major = (form.get("spock_major") or "").strip()
    spock_branch = (form.get("spock_branch") or "").strip()
    wanted_version = plan.pg_version or plan.pg_major
    wanted_spock = str(plan.spock_major)

    if role == "standby":
        if not leader_name:
            errors.append("a standby follows one Spock node — choose its leader")
        elif leader is None:
            errors.append(
                f"{leader_name!r} is not a Spock node in this cluster; pick one "
                f"of {', '.join(n.name for n in plan.spock_nodes)}"
            )
        if pg_version or spock_major or spock_branch:
            errors.append("a standby is a byte-for-byte copy of its leader and "
                          "runs exactly what the leader runs")
        if leader is not None:
            wanted_version = leader.pg_version or wanted_version
            wanted_spock = leader.spock_major or wanted_spock
    else:
        try:
            wanted_version = add_node.check_version(plan, pg_version)
        except add_node.AddNodeError as exc:
            errors.append(str(exc))
        try:
            wanted_spock, notes = add_node.check_spock(plan, spock_major)
            warnings.extend(notes)
        except add_node.AddNodeError as exc:
            errors.append(str(exc))
        if plan.deploy_mode == "source":
            if not pg_server_management.is_exact_version(wanted_version):
                errors.append(
                    f"a source build compiles one exact version — "
                    f"{wanted_version}.1, or {wanted_version}beta3 for a major "
                    f"with no release yet"
                )
            spock_branch = spock_branch or (
                (plan.source_build or {}).get("spock_branch")
                if wanted_spock == str(plan.spock_major) else ""
            ) or source_build.default_spock_branch(wanted_spock)
        elif spock_branch:
            warnings.append(
                f"--spock-branch only applies to a source build; this cluster "
                f"installs packages, so spock{wanted_spock} comes from the "
                f"{plan.repo_channel} channel"
            )

    # --- sync, which belongs to a standby's scope --------------------
    sync_mode = (form.get("sync_mode") or "").strip()
    if sync_mode and role != "standby":
        errors.append("the replication mode describes how a leader replicates "
                      "to its standbys, so it applies to a standby")

    wanted_major = pg_server_management.major_of(wanted_version)
    ports = add_node.next_ports(plan, host.name) if host else (None, None)
    node = {
        "name": name,
        "role": "standby" if role == "standby" else "spock",
        "host": host.name if host else None,
        "address": host.address if host else None,
        "host_is_new": bool(host and is_new),
        "pg_port": ports[0],
        "restapi_port": ports[1],
        "data_dir": f"{plan.data_root}/{name}",
        "scope": (f"{plan.cluster_name}-{form.get('leader')}"
                  if role == "standby" else f"{plan.cluster_name}-{name}"),
        "pg_version": wanted_version,
        "bin_dir": add_node.bin_dir_for(plan, host, wanted_major) if host else None,
        "spock_major": wanted_spock,
        "spock_branch": spock_branch if plan.deploy_mode == "source" else "",
        "builds_from_source": (
            plan.deploy_mode == "source" and role != "standby"
        ),
        "sync_mode": sync_mode or ("async" if role == "standby" else ""),
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings, "node": node}


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------


def build_blueprint(runner, inventory_path=None, changes_allowed=True,
                    db_password=None):
    """Routes for the add-node page and its API."""
    api = Blueprint("nodes", __name__)

    def cluster_of(payload=None):
        requested = (payload or {}).get("cluster") or request.args.get("cluster")
        requested = (requested or "").strip()
        clusters = state.list_clusters()
        if requested and requested in clusters:
            return requested
        return clusters[0] if clusters else None

    @api.route("/add-node")
    def page():
        clusters = state.list_clusters()
        return render_template("add_node.html",
                               cluster=cluster_of(),
                               clusters=clusters,
                               changes_allowed=changes_allowed)

    @api.route("/api/add-node/options")
    def options():
        cluster = cluster_of()
        if cluster is None:
            return jsonify({"error": "no clusters have been deployed yet"}), 404
        try:
            payload = cluster_options(cluster, inventory_path)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        payload["changes_allowed"] = changes_allowed
        payload["running_job"] = (runner.running().view()
                                  if runner.running() else None)
        return jsonify(payload)

    @api.route("/api/add-node/preview", methods=["POST"])
    def preview_route():
        form = request.get_json(silent=True) or {}
        cluster = cluster_of(form)
        if cluster is None:
            return jsonify({"error": "no clusters have been deployed yet"}), 404
        try:
            return jsonify(preview(cluster, form, inventory_path))
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

    @api.route("/api/add-node", methods=["POST"])
    def submit():
        if not changes_allowed:
            return jsonify({
                "error": "this dashboard is read-only. Start it with "
                         "--allow-changes to add nodes from the browser."
            }), 403

        form = request.get_json(silent=True) or {}
        cluster = cluster_of(form)
        if cluster is None:
            return jsonify({"error": "no clusters have been deployed yet"}), 404

        checked = preview(cluster, form, inventory_path)
        if not checked["ok"]:
            return jsonify({"error": checked["errors"][0],
                            "errors": checked["errors"]}), 400

        node = checked["node"]
        standby = node["role"] == "standby"
        summary = (f"adding {'standby' if standby else 'Spock node'} "
                   f"{node['name']} to {cluster}")

        if standby:
            def call(logger):
                return add_standby.add(
                    cluster_name=cluster,
                    leader_name=form.get("leader"),
                    host_name=form.get("host") or None,
                    db_password=db_password,
                    run_logger=logger,
                    synchronous_mode=form.get("sync_mode") or None,
                    synchronous_node_count=form.get("sync_count") or None,
                    synchronous_mode_strict=form.get("sync_strict") or None,
                )
        else:
            def call(logger):
                return add_node.add(
                    cluster_name=cluster,
                    host_name=form.get("host") or None,
                    node_name=node["name"],
                    source_node=form.get("source") or None,
                    inventory_path=inventory_path,
                    db_password=db_password,
                    run_logger=logger,
                    skip_verify=bool(form.get("skip_verify")),
                    pg_version=node["pg_version"],
                    spock_major=node["spock_major"],
                    spock_branch=node["spock_branch"],
                )

        try:
            job = runner.start("standby" if standby else "node", cluster,
                               summary, form, call)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(job.view()), 202

    @api.route("/api/jobs")
    def jobs():
        return jsonify({"jobs": [job.view(log_lines=0) for job in runner.recent()]})

    @api.route("/api/jobs/<job_id>")
    def job_view(job_id):
        job = runner.get(job_id)
        if job is None:
            return jsonify({"error": f"no job {job_id}"}), 404
        return jsonify(job.view(log_lines=int(request.args.get("lines", 80))))

    return api
