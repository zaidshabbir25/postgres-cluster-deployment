#!/usr/bin/env python3
"""Operator-facing control of the services a node runs.

The pgEdge CLI's `service` module. One important difference: on a
Patroni-managed cluster you must not start or stop PostgreSQL directly.
Patroni owns the postmaster — stopping it behind Patroni's back looks like a
crash, and Patroni either restarts it immediately or fails over. So "restart
postgres" here means "ask Patroni to restart it", and stopping a node means
stopping its Patroni instance.

Components:
    patroni   the per-node Patroni instance (unit patroni-<node>)
    postgres  the postmaster, operated only through patronictl
    etcd      the DCS, on the hosts that are members
"""

from aspects import etcd_management, patroni_management, service_management

COMPONENTS = ("patroni", "postgres", "etcd")


def _patronictl(executor, node):
    return patroni_management.patronictl_binary(executor, node=node.name)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status(executor, plan, node, host=None):
    """Everything running on behalf of one node."""
    unit = patroni_management.unit_name(node.name)
    entry = {
        "node": node.name,
        "host": node.host,
        "patroni": {
            "unit": unit,
            "active": service_management.is_active(executor, unit, node=node.name),
            "enabled": None,
        },
    }

    ok, output = executor.try_run(
        f"systemctl is-enabled {unit} 2>/dev/null", node=node.name
    )
    entry["patroni"]["enabled"] = output.strip() if ok else "unknown"

    rest = patroni_management.rest_health(executor, node, node_name=node.name)
    entry["postgres"] = {
        "role": (rest or {}).get("role"),
        "state": (rest or {}).get("state"),
        "timeline": (rest or {}).get("timeline"),
        "port": node.pg_port,
        "responding": rest is not None,
    }

    if host is not None and host.is_etcd_member:
        entry["etcd"] = {
            "unit": etcd_management.UNIT,
            "active": service_management.is_active(
                executor, etcd_management.UNIT, node=node.name
            ),
        }
    return entry


def cluster_status(executor_for, plan):
    """Service status for every node, plus etcd per member host."""
    entries = []
    for node in plan.nodes:
        executor = executor_for(node)
        entries.append(status(executor, plan, node, host=plan.host(node.host)))
    return entries


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def start(executor, plan, node, component="patroni"):
    """Start a component. Returns (ok, message)."""
    if component == "patroni":
        message = patroni_management.start(executor, plan, node)
        return True, message

    if component == "postgres":
        # Patroni starts the postmaster as part of its own loop; the only
        # supported way to get one running is to have Patroni running.
        unit = patroni_management.unit_name(node.name)
        if not service_management.is_active(executor, unit, node=node.name):
            return False, (
                f"{unit} is not running. PostgreSQL is started by Patroni, so "
                f"start Patroni instead: --component patroni"
            )
        return True, "PostgreSQL is managed by Patroni and already supervised"

    if component == "etcd":
        host = plan.host(node.host)
        if not host.is_etcd_member:
            return False, f"{node.host} is not an etcd member"
        members = plan.etcd_hosts
        detail = etcd_management.start(executor, host, members, node=node.name)
        return True, detail

    return False, f"unknown component {component!r}; expected one of {COMPONENTS}"


def stop(executor, plan, node, component="patroni"):
    """Stop a component.

    Stopping Patroni on a scope leader hands the scope to a standby if one
    exists; with no standby the scope simply has no leader until it is started
    again. Either way the caller is told what happened.
    """
    if component == "patroni":
        patroni_management.stop(executor, node.name)
        note = ""
        if node.is_spock and node.standbys:
            note = (
                f" — scope {node.scope} will fail over to "
                f"{', '.join(node.standbys)}, after which {node.name}'s Spock "
                f"peers need retargeting (--repair-failover {node.name})"
            )
        elif node.is_spock:
            note = f" — scope {node.scope} now has no leader"
        return True, f"patroni-{node.name} stopped{note}"

    if component == "postgres":
        return False, (
            "PostgreSQL cannot be stopped directly on a Patroni-managed node: "
            "Patroni would treat it as a crash and restart it or fail over. "
            "Stop Patroni instead, or use --component patroni."
        )

    if component == "etcd":
        host = plan.host(node.host)
        if not host.is_etcd_member:
            return False, f"{node.host} is not an etcd member"
        etcd_management.stop(executor, node=node.name)
        remaining = len(plan.etcd_hosts) - 1
        quorum = len(plan.etcd_hosts) // 2 + 1
        note = ""
        if remaining < quorum:
            note = (
                f" — WARNING: only {remaining} of {len(plan.etcd_hosts)} members "
                f"left, below the quorum of {quorum}. Patroni cannot elect "
                f"leaders until quorum returns."
            )
        return True, f"etcd stopped on {node.host}{note}"

    return False, f"unknown component {component!r}"


def restart(executor, plan, node, component="patroni"):
    """Restart a component through the right mechanism."""
    if component == "postgres":
        # patronictl restart is the supported path: it coordinates through the
        # DCS so the rest of the scope knows the restart is intentional.
        binary = _patronictl(executor, node)
        ok, output = executor.try_run(
            f"{binary} -c {node.config_file} restart {node.scope} {node.name} "
            f"--force",
            node=node.name, timeout=600,
        )
        return ok, output.strip() or f"PostgreSQL restarted on {node.name}"

    if component == "patroni":
        unit = patroni_management.unit_name(node.name)
        ok, output = service_management.restart(executor, unit, node=node.name)
        return ok, output.strip() or f"{unit} restarted"

    if component == "etcd":
        ok, output = service_management.restart(
            executor, etcd_management.UNIT, node=node.name
        )
        return ok, output.strip() or "etcd restarted"

    return False, f"unknown component {component!r}"


def reload(executor, plan, node, component="postgres"):
    """Reload configuration without a restart."""
    if component == "postgres":
        binary = _patronictl(executor, node)
        ok, output = executor.try_run(
            f"{binary} -c {node.config_file} reload {node.scope} {node.name} "
            f"--force",
            node=node.name,
        )
        return ok, output.strip() or f"configuration reloaded on {node.name}"

    if component == "patroni":
        unit = patroni_management.unit_name(node.name)
        ok, output = executor.try_run(f"systemctl reload {unit}", node=node.name)
        return ok, output.strip() or f"{unit} reloaded"

    return False, f"{component} does not support reload"


def enable(executor, plan, node, component="patroni"):
    unit = (patroni_management.unit_name(node.name) if component == "patroni"
            else etcd_management.UNIT)
    ok, output = executor.try_run(f"systemctl enable {unit}", node=node.name)
    return ok, output.strip() or f"{unit} will start on boot"


def disable(executor, plan, node, component="patroni"):
    unit = (patroni_management.unit_name(node.name) if component == "patroni"
            else etcd_management.UNIT)
    ok, output = executor.try_run(f"systemctl disable {unit}", node=node.name)
    return ok, output.strip() or f"{unit} will not start on boot"


def logs(executor, plan, node, component="patroni", lines=50):
    """Recent log output for a component."""
    if component == "patroni":
        return patroni_management.logs(executor, node.name, lines=lines)
    if component == "etcd":
        return service_management.journal(
            executor, etcd_management.UNIT, lines=lines, node=node.name
        )
    if component == "postgres":
        # Patroni logs the postmaster's output into its own journal.
        return patroni_management.logs(executor, node.name, lines=lines)
    return f"unknown component {component!r}"


# ---------------------------------------------------------------------------
# Patroni-specific operations
# ---------------------------------------------------------------------------


def switchover(executor, plan, node, candidate=None):
    """Hand a scope's leadership to a standby, without data loss."""
    if not node.is_spock:
        return False, f"{node.name} is not a scope leader"
    if not node.standbys:
        return False, (
            f"scope {node.scope} has no standby to switch over to — add one "
            f"with: add-standby --leader {node.name}"
        )
    candidate = candidate or node.standbys[0]
    ok, output = patroni_management.switchover(
        executor, node, candidate, node_name=node.name
    )
    message = output.strip()
    if ok:
        message += (
            f"\n{candidate} now leads {node.scope}. Retarget {node.name}'s Spock "
            f"peers with: --repair-failover {node.name}"
        )
    return ok, message


def reinitialise(executor, plan, node, member):
    """Rebuild a member from its current leader."""
    ok, output = patroni_management.reinitialise(
        executor, node, member, node_name=node.name
    )
    return ok, output.strip()
