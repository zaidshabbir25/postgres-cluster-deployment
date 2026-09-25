#!/usr/bin/env python3
"""The deploy form's backend: plan a cluster, then build it.

Same shape as the add-node form, and for the same reason: the rules live in
`topology.plan_cluster` and `inventory.load`, and this module only arranges
them for a browser. The preview *is* a plan — the identical call the CLI makes
for `plan` and again for `deploy` — so the node table on the page is the table
the deployment will produce, warnings and all.

A deployment takes minutes, or the better part of an hour when it compiles
PostgreSQL, so the POST starts a background job on the shared runner (one
cluster change at a time) and the page follows its steps.
"""

from flask import Blueprint, jsonify, render_template, request

from aspects import inventory, pg_server_management, source_build, state
from deployment import deploy_cluster, topology

from dashboard.node_api import SPOCK_MAJORS, published_versions

PG_MAJORS = ("16", "17", "18", "19")
MAX_NODES = 32


def options(inventory_path=None):
    """Everything the form needs: hosts to choose from, and what may run."""
    hosts, defaults = [], {}
    problem = ""
    try:
        loaded, defaults = inventory.load(inventory_path)
        hosts = [
            {"name": host.name, "address": host.address,
             "username": host.username, "local": host.local,
             "description": host.description}
            for host in loaded
        ]
    except inventory.InventoryError as exc:
        problem = str(exc)

    return {
        "hosts": hosts,
        "inventory_problem": problem,
        "deployed": state.list_clusters(),
        "pg_majors": list(PG_MAJORS),
        "pg_versions": published_versions(PG_MAJORS),
        "spock_majors": list(SPOCK_MAJORS),
        "spock_branches": {major: source_build.default_spock_branch(major)
                           for major in SPOCK_MAJORS},
        "defaults": {
            "cluster_name": defaults.get("cluster_name", "pgedge"),
            "node_count": int(defaults.get("node_count", 2)),
            "pg_major": str(defaults.get("pg_major", "17")),
            "spock_major": str(defaults.get("spock_major", "50")),
            "repo_channel": defaults.get("repo_channel", "release"),
            "db_name": defaults.get("db_name", "postgres"),
            "db_user": defaults.get("db_user", "postgres"),
            "base_port": int(defaults.get("base_port", 5432)),
            "base_restapi_port": int(defaults.get("base_restapi_port", 8008)),
            "data_root": defaults.get("data_root", topology.DEFAULT_DATA_ROOT),
        },
    }


def _number(form, key, fallback):
    """A number from the form, where 0 means 0 rather than "not given".

    `int(value or fallback)` reads a deliberate zero as absent, which turns
    "no nodes" into the default two — a validation error the operator never
    sees becomes a cluster they did not ask for.
    """
    value = form.get(key)
    if value in (None, ""):
        return int(fallback)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def build_options(form, inventory_path=None):
    """Turn the form into the options dict `deploy_cluster.deploy` expects."""
    mode = "source" if form.get("deploy_mode") == "source" else "packages"
    standby_of = [name for name in (form.get("standby_of") or []) if name]
    chosen = [name for name in (form.get("hosts") or []) if name]

    options_dict = {
        "inventory": inventory_path,
        "hosts": chosen,
        "cluster_name": (form.get("cluster_name") or "pgedge").strip(),
        "node_count": _number(form, "node_count", 2),
        "standby_of": standby_of,
        "deploy_mode": mode,
        "pg_major": str(form.get("pg_major") or "17"),
        "pg_version": (form.get("pg_version") or "").strip(),
        "spock_major": str(form.get("spock_major") or "50"),
        "repo_channel": form.get("repo_channel") or "release",
        "db_name": (form.get("db_name") or "postgres").strip(),
        "db_user": (form.get("db_user") or "postgres").strip(),
        "db_password": form.get("db_password") or None,
        "base_port": _number(form, "base_port", 5432),
        "base_restapi_port": _number(form, "base_restapi_port", 8008),
        "data_root": (form.get("data_root") or topology.DEFAULT_DATA_ROOT).strip(),
        "clean": bool(form.get("clean")),
        "skip_verify": bool(form.get("skip_verify")),
        "synchronous_mode": form.get("sync_mode") or "off",
        "synchronous_node_count": _number(form, "sync_count", 1),
        "synchronous_mode_strict": bool(form.get("sync_strict")),
    }
    if mode == "source":
        options_dict["source_build"] = {
            "pg_version": options_dict["pg_version"],
            "spock_branch": (form.get("spock_branch") or "").strip()
                            or source_build.default_spock_branch(
                                options_dict["spock_major"]),
        }
    return options_dict


def preview(form, inventory_path=None):
    """Plan the cluster this form describes, or say why it cannot be planned."""
    errors, warnings, nodes = [], [], []
    wanted = build_options(form, inventory_path)

    name = wanted["cluster_name"]
    if not name.replace("-", "").replace("_", "").isalnum():
        errors.append(f"cluster name {name!r} should be letters, digits, "
                      f"dashes or underscores")
    if name in state.list_clusters() and not wanted["clean"]:
        errors.append(
            f"a cluster named {name!r} is already deployed. Choose another "
            f"name, or tick 'wipe any previous deployment' to rebuild it."
        )
    if not 1 <= wanted["node_count"] <= MAX_NODES:
        errors.append(f"between 1 and {MAX_NODES} nodes, not "
                      f"{wanted['node_count']}")
    if wanted["deploy_mode"] == "source" and not \
            pg_server_management.is_exact_version(wanted["pg_version"]):
        errors.append(
            "a source build compiles one exact version — 17.11, or 19beta3 "
            "for a major with no release yet"
        )

    try:
        hosts, _ = inventory.load(inventory_path)
    except inventory.InventoryError as exc:
        return {"ok": False, "errors": errors + [str(exc)], "warnings": [],
                "nodes": [], "etcd": []}

    by_name = {host.name: host for host in hosts}
    chosen = [by_name[n] for n in wanted["hosts"] if n in by_name] or hosts
    if not chosen:
        errors.append("choose at least one host")

    if not errors:
        try:
            # The same call the deployment makes, so what is shown is what is
            # built — including the warnings it would print.
            plan, plan_warnings = topology.plan_cluster(
                hosts=[_copy(host) for host in chosen],
                cluster_name=name,
                node_count=wanted["node_count"],
                standby_of=wanted["standby_of"],
                db_name=wanted["db_name"],
                db_user=wanted["db_user"],
                pg_major=wanted["pg_major"],
                pg_version=wanted["pg_version"],
                spock_major=wanted["spock_major"],
                repo_channel=wanted["repo_channel"],
                deploy_mode=wanted["deploy_mode"],
                base_pg_port=wanted["base_port"],
                base_restapi_port=wanted["base_restapi_port"],
                data_root=wanted["data_root"],
                synchronous_mode=wanted["synchronous_mode"],
                synchronous_node_count=wanted["synchronous_node_count"],
                synchronous_mode_strict=wanted["synchronous_mode_strict"],
            )
        except topology.TopologyError as exc:
            errors.append(str(exc))
        else:
            warnings = plan_warnings
            nodes = [
                {"name": node.name, "role": node.role, "host": node.host,
                 "address": node.address, "pg_port": node.pg_port,
                 "restapi_port": node.restapi_port, "scope": node.scope,
                 "follows": node.leader or ""}
                for node in plan.nodes
            ]
            etcd = plan.etcd_endpoints

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "nodes": nodes,
        "etcd": etcd if not errors else [],
        "node_names": topology.node_names(wanted["node_count"]),
        "summary": {
            "cluster": name,
            "mode": wanted["deploy_mode"],
            "postgres": wanted["pg_version"] or wanted["pg_major"],
            "spock": wanted["spock_major"],
            "hosts": [host.name for host in chosen],
        },
    }


def _copy(host):
    """A throwaway Host, so planning cannot mark the cached inventory."""
    from aspects.cluster_model import Host

    return Host(name=host.name, address=host.address, username=host.username,
                key_file=host.key_file, port=host.port, local=host.local,
                description=host.description)


def build_blueprint(runner, inventory_path=None, changes_allowed=True,
                    db_password=None):
    api = Blueprint("deploy", __name__)

    @api.route("/deploy")
    def page():
        return render_template("deploy.html",
                               clusters=state.list_clusters(),
                               changes_allowed=changes_allowed)

    @api.route("/api/deploy/options")
    def options_route():
        payload = options(inventory_path)
        payload["changes_allowed"] = changes_allowed
        payload["running_job"] = (runner.running().view()
                                  if runner.running() else None)
        return jsonify(payload)

    @api.route("/api/deploy/preview", methods=["POST"])
    def preview_route():
        return jsonify(preview(request.get_json(silent=True) or {},
                               inventory_path))

    @api.route("/api/inventory/hosts", methods=["POST"])
    def add_host():
        if not changes_allowed:
            return jsonify({"error": "this dashboard is read-only"}), 403
        try:
            record = inventory.add_host(request.get_json(silent=True) or {},
                                        inventory_path)
        except (inventory.InventoryError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"host": record}), 201

    @api.route("/api/deploy", methods=["POST"])
    def submit():
        if not changes_allowed:
            return jsonify({
                "error": "this dashboard is read-only. Start it with "
                         "--allow-changes to deploy from the browser."
            }), 403

        form = request.get_json(silent=True) or {}
        checked = preview(form, inventory_path)
        if not checked["ok"]:
            return jsonify({"error": checked["errors"][0],
                            "errors": checked["errors"]}), 400

        wanted = build_options(form, inventory_path)
        if db_password and not wanted.get("db_password"):
            wanted["db_password"] = db_password
        cluster = wanted["cluster_name"]
        summary = (f"deploying {cluster}: {wanted['node_count']} node(s) on "
                   f"{len(checked['summary']['hosts'])} host(s)")

        def call(logger):
            return deploy_cluster.deploy(wanted, run_logger=logger)

        try:
            job = runner.start("deploy", cluster, summary, form, call)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(job.view()), 202

    return api
