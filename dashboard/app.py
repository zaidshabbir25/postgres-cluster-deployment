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
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from flask import Flask, jsonify, render_template, request
except ImportError:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Flask is required for the dashboard.\n"
        "  python3 -m venv venv && source venv/bin/activate\n"
        "  pip install -r requirements.txt"
    )

from aspects import health, inventory, state

DEFAULT_INTERVAL = 20
MIN_INTERVAL = 5


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


def create_app(interval=DEFAULT_INTERVAL, db_password=None, default_cluster=None):
    app = Flask(__name__)
    registry = WatcherRegistry(interval=interval, db_password=db_password)
    app.config["REGISTRY"] = registry

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
    def on_error(exc):  # pragma: no cover - defensive
        return jsonify({"error": f"dashboard error: {exc}"}), 500

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
    args = parser.parse_args(argv)

    clusters = state.list_clusters()
    if not clusters:
        print(
            "No deployed clusters found in configuration/clusters/.\n"
            "Deploy one first with ./pg_deploy_cluster.sh — the dashboard reads "
            "the state file that deployment writes.",
            file=sys.stderr,
        )
        return 1

    app = create_app(
        interval=args.interval,
        db_password=os.environ.get("PG_CLUSTER_DB_PASSWORD"),
        default_cluster=args.cluster,
    )

    print(f"Dashboard on http://{args.host}:{args.port}")
    print(f"Clusters: {', '.join(clusters)}")
    print(f"Polling every {max(MIN_INTERVAL, args.interval)}s")
    # Threaded so a slow first collection cannot block the page load.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True,
            use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
