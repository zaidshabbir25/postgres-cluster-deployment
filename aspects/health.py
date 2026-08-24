#!/usr/bin/env python3
"""Collect the whole cluster's health into one serialisable snapshot.

Both the dashboard and the deployment report read this. It is written to be
resilient rather than strict: a host that has gone away, a node that will not
answer, or a Patroni REST API that times out each degrade their own entry and
leave the rest of the snapshot intact — a monitoring view that dies on the
first unreachable node is useless exactly when it is needed.

Status vocabulary, from best to worst:
    ok        everything expected is present and replicating
    degraded  serving, but something is wrong (a replica down, lag, a slot idle)
    down      not reachable or not serving
    unknown   could not be determined
"""

from datetime import datetime, timezone

from aspects import etcd_management, patroni_management, spock_management
from aspects import pg_server_management
from aspects.ssh_executor import build_executor

STATUS_ORDER = {"ok": 0, "degraded": 1, "unknown": 2, "down": 3}


def worst(*statuses):
    """Combine statuses, keeping the most severe."""
    present = [s for s in statuses if s]
    if not present:
        return "unknown"
    return max(present, key=lambda s: STATUS_ORDER.get(s, 2))


class ExecutorPool:
    """Reuse one connection per host across a whole health sweep."""

    def __init__(self, plan, run_logger=None):
        self.plan = plan
        self._log = run_logger
        self._pool = {}
        self.failures = {}

    def for_host(self, host_name):
        if host_name in self._pool:
            return self._pool[host_name]
        if host_name in self.failures:
            return None

        host = self.plan.host(host_name)
        try:
            executor = build_executor(
                {
                    "name": host.name,
                    "host": host.address,
                    "username": host.username,
                    "key_file": host.key_file,
                    "port": host.port,
                    "local": host.local,
                },
                run_logger=self._log,
            )
            executor.connect()
            self._pool[host_name] = executor
            return executor
        except Exception as exc:
            self.failures[host_name] = str(exc)
            return None

    def for_node(self, node):
        return self.for_host(node.host)

    def close(self):
        for executor in self._pool.values():
            try:
                executor.close()
            except Exception:
                pass
        self._pool.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Per-layer collectors
# ---------------------------------------------------------------------------


def collect_host(executor, host):
    """Basic liveness and capacity for one machine."""
    if executor is None:
        return {
            "name": host.name,
            "address": host.address,
            "status": "down",
            "reachable": False,
            "error": "SSH connection failed",
        }

    _, uptime = executor.try_run("uptime 2>/dev/null || true")
    _, load = executor.try_run("cat /proc/loadavg 2>/dev/null || true")
    _, disk = executor.try_run(
        "df -h --output=target,pcent,avail / /var 2>/dev/null | tail -n +2 || true"
    )
    _, memory = executor.try_run(
        "free -m 2>/dev/null | awk '/^Mem:/ {print $2\" \"$3\" \"$7}' || true"
    )

    load_average = None
    parts = load.split()
    if parts:
        try:
            load_average = float(parts[0])
        except ValueError:
            load_average = None

    disk_rows, disk_status = [], "ok"
    for line in disk.strip().splitlines():
        fields = line.split()
        if len(fields) >= 3:
            used = fields[1].rstrip("%")
            row = {"mount": fields[0], "used_pct": used, "available": fields[2]}
            disk_rows.append(row)
            try:
                if int(used) >= 90:
                    disk_status = "degraded"
            except ValueError:
                pass

    total_mb = used_mb = available_mb = None
    memory_fields = memory.split()
    if len(memory_fields) == 3:
        try:
            total_mb, used_mb, available_mb = (int(x) for x in memory_fields)
        except ValueError:
            pass

    return {
        "name": host.name,
        "address": host.address,
        "status": disk_status,
        "reachable": True,
        "platform": host.platform.get("pretty", "") or host.family,
        "arch": host.platform.get("arch", ""),
        "uptime": uptime.strip(),
        "load_average": load_average,
        "disks": disk_rows,
        "memory_mb": {"total": total_mb, "used": used_mb, "available": available_mb},
        "is_etcd_member": host.is_etcd_member,
    }


def collect_etcd(pool, plan):
    """etcd cluster health, asked of the first member we can reach."""
    members = plan.etcd_hosts
    if not members:
        return {"status": "unknown", "note": "no etcd members recorded in the plan",
                "endpoints": list(plan.etcd_endpoints)}

    for host in members:
        executor = pool.for_host(host.name)
        if executor is None:
            continue
        status = etcd_management.cluster_status(executor, members, node=host.name)
        status["status"] = "ok" if status["ok"] else "degraded"
        status["queried_from"] = host.name
        if status["member_count"] == 1:
            status["note"] = "single-member etcd — no fault tolerance"
        return status

    return {
        "status": "down",
        "note": "no etcd member host was reachable",
        "endpoints": list(plan.etcd_endpoints),
    }


def collect_node(executor, plan, node):
    """One PostgreSQL node: reachability, role, replication."""
    entry = {
        "name": node.name,
        "role": node.role,
        "host": node.host,
        "address": node.address,
        "pg_port": node.pg_port,
        "restapi_port": node.restapi_port,
        "scope": node.scope,
        "leader_of": node.standbys,
        "follows": node.leader,
        "status": "unknown",
        "patroni_role": None,
        "patroni_state": None,
        "accepting_writes": None,
        "problems": [],
    }

    if executor is None:
        entry["status"] = "down"
        entry["problems"].append("host unreachable over SSH")
        return entry

    ready, detail = pg_server_management.wait_for_ready(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        timeout=6, interval=3, node=node.name,
    )
    entry["postgres_reachable"] = ready
    if not ready:
        entry["status"] = "down"
        entry["problems"].append(f"PostgreSQL not accepting connections: {detail}")
        # Patroni may still be able to explain why.
        rest = patroni_management.rest_health(executor, node, node_name=node.name)
        if rest:
            entry["patroni_role"] = rest.get("role")
            entry["patroni_state"] = rest.get("state")
        return entry

    entry["pg_version"] = pg_server_management.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        "SHOW server_version;", dbname=plan.db_name, node=node.name,
    )
    in_recovery = spock_management.is_in_recovery(executor, plan, node)
    entry["in_recovery"] = in_recovery
    entry["accepting_writes"] = not in_recovery

    rest = patroni_management.rest_health(executor, node, node_name=node.name)
    if rest:
        entry["patroni_role"] = rest.get("role")
        entry["patroni_state"] = rest.get("state")
        entry["timeline"] = rest.get("timeline")
        replication = rest.get("replication") or []
        entry["patroni_replication"] = replication
    else:
        entry["problems"].append("Patroni REST API did not respond")

    entry["slots"] = spock_management.replication_slots(executor, plan, node)
    entry["streaming"] = spock_management.streaming_peers(executor, plan, node)

    if node.is_spock:
        entry["spock_version"] = spock_management.spock_version(executor, plan, node)
        entry["spock_nodes_visible"] = spock_management.node_table(executor, plan, node)
        entry["subscriptions"] = spock_management.subscription_status(
            executor, plan, node
        )

        expected_peers = len(plan.spock_nodes) - 1
        replicating = [
            s for s in entry["subscriptions"]
            if (s.get("status") or "").lower() in ("replicating", "running")
        ]
        entry["subscriptions_replicating"] = len(replicating)
        entry["subscriptions_expected"] = expected_peers

        missing = sorted(
            {n.name for n in plan.spock_nodes} - set(entry["spock_nodes_visible"] or [])
        )
        if missing:
            entry["problems"].append(
                f"not visible in spock.node: {', '.join(missing)}"
            )
        if len(replicating) < expected_peers:
            entry["problems"].append(
                f"{len(replicating)}/{expected_peers} subscriptions replicating"
            )
        if in_recovery:
            entry["problems"].append(
                "a Spock node is in recovery — Patroni has failed over and this "
                "node cannot accept writes"
            )

        # A Spock node whose scope leader has moved is serving from a different
        # address than its peers hold, so their subscriptions are stale.
        role = (entry["patroni_role"] or "").lower()
        if role and role not in ("leader", "master", "primary"):
            entry["problems"].append(
                f"Patroni reports this Spock node as {entry['patroni_role']!r}; "
                f"peers may need retargeting to the promoted standby "
                f"(pg_cluster_status.sh --repair-failover {node.name})"
            )
    else:
        if not in_recovery:
            entry["problems"].append(
                "a standby is out of recovery — it has been promoted, so its "
                "Spock leader is no longer the write target its peers expect"
            )

    idle_slots = [s for s in entry.get("slots", []) if not s.get("active")]
    if idle_slots:
        entry["problems"].append(
            "inactive replication slot(s) retaining WAL: "
            + ", ".join(f"{s['name']} ({s['retained_wal']})" for s in idle_slots)
        )

    entry["status"] = "degraded" if entry["problems"] else "ok"
    return entry


def collect_scopes(pool, plan):
    """Patroni's own view, one entry per scope."""
    scopes = {}
    for scope, members in plan.scopes().items():
        leader_node = next((m for m in members if m.is_spock), members[0])
        entry = {
            "scope": scope,
            "expected_members": [m.name for m in members],
            "status": "unknown",
            "members": [],
            "leader": None,
        }

        for candidate in [leader_node] + [m for m in members if m is not leader_node]:
            executor = pool.for_node(candidate)
            if executor is None:
                continue
            status = patroni_management.cluster_status(
                executor, candidate, node_name=candidate.name
            )
            if status["members"]:
                entry.update(status)
                entry["queried_from"] = candidate.name
                break

        if not entry["members"]:
            entry["status"] = "down"
            entry["problems"] = ["no member of this scope answered patronictl"]
        else:
            problems = []
            seen = {m["name"] for m in entry["members"]}
            missing = sorted(set(entry["expected_members"]) - seen)
            if missing:
                problems.append(f"members absent from the DCS: {', '.join(missing)}")
            if not entry.get("leader"):
                problems.append("scope has no leader")
            unhealthy = [
                f"{m['name']}={m['state']}" for m in entry["members"]
                if (m.get("state") or "").lower() not in ("running", "streaming")
            ]
            if unhealthy:
                problems.append("members not streaming: " + ", ".join(unhealthy))
            entry["problems"] = problems
            entry["status"] = "degraded" if problems else "ok"

        scopes[scope] = entry
    return scopes


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def snapshot(plan, run_logger=None, pool=None):
    """Collect a full health snapshot. Safe to call on a broken cluster."""
    owned = pool is None
    pool = pool or ExecutorPool(plan, run_logger=run_logger)

    try:
        hosts = []
        for host in plan.hosts:
            hosts.append(collect_host(pool.for_host(host.name), host))

        nodes = []
        for node in plan.nodes:
            nodes.append(collect_node(pool.for_node(node), plan, node))

        etcd = collect_etcd(pool, plan)
        scopes = collect_scopes(pool, plan)

        problems = []
        for host in hosts:
            if not host.get("reachable"):
                problems.append(f"host {host['name']} is unreachable")
        for node in nodes:
            for problem in node.get("problems", []):
                problems.append(f"{node['name']}: {problem}")
        for scope in scopes.values():
            for problem in scope.get("problems", []):
                problems.append(f"scope {scope['scope']}: {problem}")
        if etcd.get("status") not in ("ok",):
            problems.append(f"etcd: {etcd.get('note') or etcd.get('status')}")

        overall = worst(
            *[h["status"] for h in hosts],
            *[n["status"] for n in nodes],
            *[s["status"] for s in scopes.values()],
            etcd.get("status"),
        )

        return {
            "cluster": plan.cluster_name,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "status": overall,
            "summary": {
                "hosts": len(hosts),
                "hosts_up": sum(1 for h in hosts if h.get("reachable")),
                "nodes": len(nodes),
                "nodes_ok": sum(1 for n in nodes if n["status"] == "ok"),
                "spock_nodes": len(plan.spock_nodes),
                "standby_nodes": len(plan.standby_nodes),
                "scopes": len(scopes),
                "problem_count": len(problems),
            },
            "hosts": hosts,
            "nodes": nodes,
            "scopes": scopes,
            "etcd": etcd,
            "problems": problems,
            "plan": plan.to_dict(),
        }
    finally:
        if owned:
            pool.close()


def text_report(snap):
    """Render a snapshot as terminal text."""
    lines = [
        f"Cluster {snap['cluster']} — {snap['status'].upper()} "
        f"({snap['collected_at']})",
        "",
        f"{'NODE':<10} {'ROLE':<9} {'PATRONI':<10} {'STATE':<12} "
        f"{'WRITES':<7} {'SUBS':<7} STATUS",
    ]
    for node in snap["nodes"]:
        subs = (
            f"{node.get('subscriptions_replicating', '-')}"
            f"/{node.get('subscriptions_expected', '-')}"
            if node["role"] == "spock" else "-"
        )
        writes = {True: "yes", False: "no", None: "?"}[node.get("accepting_writes")]
        lines.append(
            f"{node['name']:<10} {node['role']:<9} "
            f"{str(node.get('patroni_role') or '-'):<10} "
            f"{str(node.get('patroni_state') or '-'):<12} "
            f"{writes:<7} {subs:<7} {node['status']}"
        )

    lines += ["", f"etcd: {snap['etcd'].get('status')} "
                  f"({snap['etcd'].get('healthy_count', '?')}/"
                  f"{snap['etcd'].get('member_count', '?')} healthy)"]

    if snap["problems"]:
        lines += ["", f"Problems ({len(snap['problems'])}):"]
        lines += [f"  - {problem}" for problem in snap["problems"]]
    else:
        lines += ["", "No problems detected."]
    return "\n".join(lines)
