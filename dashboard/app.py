#!/usr/bin/env python3
"""Live cluster dashboard.

Health collection walks every host over SSH and runs a handful of queries per
node, which takes seconds — far too slow to do inside a request. So a
background poller refreshes each watched cluster on an interval and the web
layer only ever serves the cached snapshot. A browser hitting refresh never
blocks on SSH, and ten open tabs cost exactly as much as one.

Run it with ./pg_dashboard.sh (which sets up the venv and passes flags through).
"""

import argparse
import hmac
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from flask import (Flask, jsonify, make_response, redirect, render_template,
                       request)
except ImportError:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Flask is required for the dashboard.\n"
        "  python3 -m venv venv && source venv/bin/activate\n"
        "  pip install -r requirements.txt"
    )

from aspects import health, inventory, state
from dashboard import deploy_api, node_api

DEFAULT_INTERVAL = 20
MIN_INTERVAL = 5

# Name of the cookie a browser keeps once it has presented the token, so the
# token itself stays out of every later URL (and out of the address bar).
TOKEN_COOKIE = "pgcluster_token"


class ClusterWatcher:
    """Polls one cluster in the background and holds its latest snapshot."""

    def __init__(self, cluster_name, interval=DEFAULT_INTERVAL, db_password=None):
        self.cluster_name = cluster_name
        self.interval = max(MIN_INTERVAL, int(interval))
        self.db_password = db_password

        self._lock = threading.Lock()
        self._snapshot = None
        self._error = None
        self._collecting = False
        self._last_duration = None
        self._refresh_now = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"watch-{self.cluster_name}", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._refresh_now.set()

    def request_refresh(self):
        self._refresh_now.set()

    # ------------------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            self._collect()
            # Wake early when someone asks for a refresh, otherwise sleep out
            # the interval.
            self._refresh_now.wait(timeout=self.interval)
            self._refresh_now.clear()

    def _collect(self):
        with self._lock:
            self._collecting = True
        started = time.time()
        try:
            plan, metadata = state.load(self.cluster_name)
            plan.db_password = self._resolve_password(plan)
            snapshot = health.snapshot(plan)
            snapshot["metadata"] = metadata
            snapshot["poll_interval"] = self.interval
            with self._lock:
                self._snapshot = snapshot
                self._error = None
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._collecting = False
                self._last_duration = round(time.time() - started, 2)

    def _resolve_password(self, plan):
        if self.db_password:
            return self.db_password
        try:
            _, defaults = inventory.load()
        except Exception:
            defaults = {}
        return state.resolve_password(plan, inventory_defaults=defaults)

    # ------------------------------------------------------------------

    def view(self):
        """The payload the API returns. Never raises."""
        with self._lock:
            snapshot = self._snapshot
            error = self._error
            collecting = self._collecting
            duration = self._last_duration

        if snapshot is None:
            return {
                "cluster": self.cluster_name,
                "status": "unknown",
                "collecting": collecting,
                "error": error or "waiting for the first health collection",
                "nodes": [],
                "hosts": [],
                "scopes": {},
                "problems": [],
                "summary": {},
            }

        payload = dict(snapshot)
        payload["collecting"] = collecting
        payload["collection_seconds"] = duration
        payload["stale_seconds"] = _age_seconds(snapshot.get("collected_at"))
        # A collection that fails after a good one should not blank the page —
        # show the last known state and say the refresh is failing.
        payload["error"] = error
        return payload


def _age_seconds(timestamp):
    if not timestamp:
        return None
    try:
        collected = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - collected).total_seconds(), 1)


class WatcherRegistry:
    """One watcher per cluster, created on first request."""

    def __init__(self, interval=DEFAULT_INTERVAL, db_password=None):
        self.interval = interval
        self.db_password = db_password
        self._watchers = {}
        self._lock = threading.Lock()

    def get(self, cluster_name):
        with self._lock:
            watcher = self._watchers.get(cluster_name)
            if watcher is None:
                watcher = ClusterWatcher(
                    cluster_name, interval=self.interval,
                    db_password=self.db_password,
                )
                self._watchers[cluster_name] = watcher
                watcher.start()
            return watcher

    def stop_all(self):
        with self._lock:
            for watcher in self._watchers.values():
                watcher.stop()
            self._watchers.clear()


def install_auth(app, token):
    """Require a shared token on everything but the stylesheet and scripts.

    Deliberately simple: one token, presented as a header, a `?token=` once, or
    the cookie that first visit leaves behind. It is not a user system — it is
    the difference between "anyone who can reach the port" and "anyone who has
    the token", which is what makes a non-loopback bind defensible at all.
    Put TLS in front of it if the network between you and the VM is not one you
    control; a token over plain HTTP is visible to anyone on the path.
    """
    if not token:
        return

    @app.before_request
    def check_token():
        if request.endpoint == "static":
            return None

        presented = (
            request.headers.get("X-Dashboard-Token")
            or request.args.get("token")
            or request.cookies.get(TOKEN_COOKIE)
            or ""
        )
        if hmac.compare_digest(presented, token):
            # Seen in the URL: set the cookie and drop it from the address bar,
            # so it does not end up in history, bookmarks or a screenshot.
            if request.args.get("token"):
                stripped = {k: v for k, v in request.args.items() if k != "token"}
                query = ("?" + "&".join(f"{k}={v}" for k, v in stripped.items())
                         if stripped else "")
                response = make_response(redirect(request.path + query))
                response.set_cookie(TOKEN_COOKIE, token, httponly=True,
                                    samesite="Lax")
                return response
            return None

        if request.path.startswith("/api/"):
            return jsonify({"error": "a token is required: send it as "
                                     "X-Dashboard-Token, or open the dashboard "
                                     "once with ?token=..."}), 401
        return (
            "<h1>Token required</h1>"
            "<p>This dashboard is protected. Open it once with "
            "<code>?token=&lt;your token&gt;</code> and the browser will "
            "remember.</p>",
            401,
            {"Content-Type": "text/html; charset=utf-8"},
        )


def create_app(interval=DEFAULT_INTERVAL, db_password=None, default_cluster=None,
               changes_allowed=False, inventory_path=None, auth_token=None):
    app = Flask(__name__)
    install_auth(app, auth_token)
    registry = WatcherRegistry(interval=interval, db_password=db_password)
    app.config["REGISTRY"] = registry

    # Adding a node changes the cluster, so it is off unless asked for — see
    # --allow-changes, which main() refuses to combine with a public bind.
    runner = node_api.JobRunner()
    app.config["JOBS"] = runner
    app.register_blueprint(node_api.build_blueprint(
        runner, inventory_path=inventory_path,
        changes_allowed=changes_allowed, db_password=db_password,
    ))
    app.register_blueprint(deploy_api.build_blueprint(
        runner, inventory_path=inventory_path,
        changes_allowed=changes_allowed, db_password=db_password,
    ))

    def resolve_cluster():
        """Which cluster this request is about."""
        requested = (request.args.get("cluster") or "").strip()
        clusters = state.list_clusters()
        if requested:
            if requested not in clusters:
                return None, clusters
            return requested, clusters
        if default_cluster and default_cluster in clusters:
            return default_cluster, clusters
        return (clusters[0] if clusters else None), clusters

    @app.route("/")
    def index():
        cluster, clusters = resolve_cluster()
        return render_template(
            "index.html",
            cluster=cluster,
            clusters=clusters,
            interval=interval,
            requested=request.args.get("cluster", ""),
            changes_allowed=changes_allowed,
        )

    @app.route("/api/clusters")
    def api_clusters():
        return jsonify({"clusters": state.list_clusters()})

    @app.route("/api/health")
    def api_health():
        cluster, clusters = resolve_cluster()
        if cluster is None:
            requested = request.args.get("cluster")
            message = (
                f"no deployed cluster named {requested!r}"
                if requested else
                "no clusters have been deployed yet — run ./pg_deploy_cluster.sh"
            )
            return jsonify({
                "error": message,
                "clusters": clusters,
                "status": "unknown",
                "nodes": [], "hosts": [], "scopes": {}, "problems": [],
                "summary": {},
            }), 404

        payload = registry.get(cluster).view()
        payload["clusters"] = clusters
        return jsonify(payload)

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        cluster, _ = resolve_cluster()
        if cluster is None:
            return jsonify({"error": "no cluster selected"}), 404
        registry.get(cluster).request_refresh()
        return jsonify({"refreshing": cluster})

    @app.errorhandler(500)
    def on_error(exc):
        """Report what actually failed, not Flask's wrapper for it.

        A 500 reaches this handler as InternalServerError; the exception that
        caused it hangs off `original_exception`. Without unwrapping, every
        failure reads "the server encountered an internal error", which tells
        an operator nothing and sends them to the terminal to find the
        traceback. This is a single-operator tool on loopback (or behind a
        token), so the message itself is safe to show.
        """
        cause = getattr(exc, "original_exception", None) or exc
        app.logger.exception("%s failed", request.path, exc_info=cause)
        return jsonify({
            "error": f"{type(cause).__name__}: {cause}",
            "path": request.path,
            "hint": "the full traceback is in the terminal running "
                    "./pg_dashboard.sh",
        }), 500

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Live health dashboard for deployed Patroni + Spock clusters"
    )
    parser.add_argument("--host", default=os.environ.get("PG_DASHBOARD_HOST", "127.0.0.1"),
                        help="address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PG_DASHBOARD_PORT", 8080)),
                        help="port to listen on (default 8080)")
    parser.add_argument("--interval", type=int,
                        default=int(os.environ.get("PG_DASHBOARD_INTERVAL",
                                                   DEFAULT_INTERVAL)),
                        help=f"seconds between health polls (min {MIN_INTERVAL})")
    parser.add_argument("--cluster", default=os.environ.get("PG_CLUSTER_NAME"),
                        help="cluster to show by default")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode")
    parser.add_argument("--allow-changes", action="store_true",
                        help="enable the add-node page, which changes the "
                             "cluster (a non-loopback bind then needs a token)")
    parser.add_argument("--auth-token", default=os.environ.get("PG_DASHBOARD_TOKEN"),
                        help="require this token on every request "
                             "[$PG_DASHBOARD_TOKEN]")
    parser.add_argument("--inventory", help="host inventory to offer new hosts "
                                            "from [configuration/inventory.json]")
    args = parser.parse_args(argv)

    # Reading cluster health over a public bind is the operator's call. Letting
    # anyone who can reach the port build nodes on their machines is not — so a
    # non-loopback bind may change things only when a token guards it.
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if args.allow_changes and not loopback and not args.auth_token:
        suggestion = secrets.token_urlsafe(24)
        print(
            f"--allow-changes on {args.host} needs --auth-token: without one, "
            f"anyone who can reach this port could add nodes to your cluster.\n"
            f"\nEither tunnel instead of publishing (nothing to protect):\n"
            f"  ssh -L {args.port}:127.0.0.1:{args.port} <user>@<this-host>\n"
            f"  then open http://127.0.0.1:{args.port}/add-node\n"
            f"\nor publish it behind a token:\n"
            f"  ./pg_dashboard.sh --host {args.host} --allow-changes \\\n"
            f"      --auth-token {suggestion}\n"
            f"  then open http://{args.host}:{args.port}/add-node?token={suggestion}\n"
            f"\nA token over plain HTTP is visible to anyone on the path; put "
            f"TLS in front of it, or keep the port closed to the internet and "
            f"open it to your own address only.",
            file=sys.stderr,
        )
        return 2

    clusters = state.list_clusters()
    if not clusters and not args.allow_changes:
        print(
            "No deployed clusters found in configuration/clusters/.\n"
            "Deploy one with ./pg_deploy_cluster.sh, or start the dashboard "
            "with --allow-changes and deploy from the browser.",
            file=sys.stderr,
        )
        return 1

    app = create_app(
        interval=args.interval,
        db_password=os.environ.get("PG_CLUSTER_DB_PASSWORD"),
        default_cluster=args.cluster,
        changes_allowed=args.allow_changes,
        inventory_path=args.inventory,
        auth_token=args.auth_token,
    )

    if not loopback:
        print(f"Bound to {args.host} — reachable from the network. "
              f"{'Token required.' if args.auth_token else 'No token: anyone who can reach this port can read your cluster topology and health.'}")

    print(f"Dashboard on http://{args.host}:{args.port}")
    if args.allow_changes:
        print(f"Deploy    at  http://{args.host}:{args.port}/deploy")
        print(f"Add nodes at  http://{args.host}:{args.port}/add-node")
    print(f"Clusters: {', '.join(clusters) or 'none yet'}")
    print(f"Polling every {max(MIN_INTERVAL, args.interval)}s")
    # Threaded so a slow first collection cannot block the page load.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True,
            use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
