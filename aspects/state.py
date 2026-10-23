#!/usr/bin/env python3
"""Persisted record of what has been deployed.

After a deployment the plan is written to configuration/clusters/<name>.json.
That file is how `pg_cluster_status.sh` and the dashboard find a cluster they
did not deploy themselves — without it they would have to re-derive the port
and scope layout, and a rerun with different options would silently disagree.

The password is never stored. Anything reading a plan back supplies it from
PG_CLUSTER_DB_PASSWORD or the inventory defaults.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from aspects.cluster_model import ClusterPlan

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "configuration" / "clusters"


def state_path(cluster_name):
    return STATE_DIR / f"{cluster_name}.json"


def save(plan, extra=None):
    """Write a cluster's plan and deployment metadata."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        # Unredacted on purpose: this is how a later add-node, add-component
        # or health-check reaches the hosts again. The file lives in
        # configuration/clusters, which is gitignored, and holds a path rather
        # than a key.
        "plan": plan.to_dict(redact_key=False),
        "metadata": extra or {},
    }
    path = state_path(plan.cluster_name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def adopt_inventory_keys(plan, inventory_path=None):
    """Fill in host key paths the saved state does not have.

    State written before key paths were kept has none, and every operation on
    such a cluster would fall back to the ssh agent. The inventory still names
    them, so read them back from there rather than making the operator
    redeploy. Hosts matched by name, then by address.
    """
    missing = [host for host in plan.hosts if not host.key_file and not host.local]
    if not missing:
        return []

    try:
        from aspects import inventory as inventory_module

        hosts, defaults = inventory_module.load(inventory_path)
    except Exception:
        return []

    by_name = {h.name: h for h in hosts}
    by_address = {h.address: h for h in hosts}
    fallback = defaults.get("key_file") if isinstance(defaults, dict) else None
    adopted = []
    for host in missing:
        match = by_name.get(host.name) or by_address.get(host.address)
        key_file = (match.key_file if match else None) or fallback
        if key_file:
            host.key_file = key_file
            adopted.append(host.name)
    return adopted


def load(cluster_name, db_password=None, inventory_path=None):
    """Read a cluster's plan back. Returns (plan, metadata)."""
    path = state_path(cluster_name)
    if not path.exists():
        raise FileNotFoundError(
            f"No saved state for cluster {cluster_name!r} at {path}. "
            f"Deploy it first, or pass --cluster with one of: "
            f"{', '.join(list_clusters()) or '(none)'}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    password = db_password or os.environ.get("PG_CLUSTER_DB_PASSWORD") or ""
    plan = ClusterPlan.from_dict(payload["plan"], db_password=password)
    adopt_inventory_keys(plan, inventory_path)
    return plan, payload.get("metadata", {})


def list_clusters():
    """Names of every cluster with saved state, newest first."""
    if not STATE_DIR.exists():
        return []
    entries = sorted(
        STATE_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [p.stem for p in entries]


def latest_cluster():
    """The most recently deployed cluster, or None."""
    clusters = list_clusters()
    return clusters[0] if clusters else None


def delete(cluster_name):
    path = state_path(cluster_name)
    if path.exists():
        path.unlink()
        return True
    return False


def resolve_password(plan, explicit=None, inventory_defaults=None):
    """Find the database password for a plan loaded from disk.

    Order: explicit argument, PG_CLUSTER_DB_PASSWORD, inventory defaults, then
    the built-in default — matching how deployment resolved it originally.
    """
    if explicit:
        return explicit
    env_value = os.environ.get("PG_CLUSTER_DB_PASSWORD")
    if env_value:
        return env_value
    if inventory_defaults and inventory_defaults.get("db_password"):
        return inventory_defaults["db_password"]
    if plan.db_password and plan.db_password != "***":
        return plan.db_password
    return "postgres"
