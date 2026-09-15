#!/usr/bin/env python3
"""Patroni configuration, startup and cluster inspection.

Each Spock node is its own Patroni scope, and its standbys join that scope.
That shape matters: a Spock node is a multi-master participant whose identity
other nodes subscribe to, so it must never fail over *into* another Spock
node's data — only into its own physical standby.

    scope demo-n1 : n1 (leader)  n1s1 (replica)
    scope demo-n2 : n2 (leader)  n2s1 (replica)
            n1 <--- spock multi-master ---> n2

Patroni bootstraps each leader itself rather than adopting a cluster created by
initdb: bootstrap is the path Patroni is designed for, and it puts the Spock
GUCs in the DCS where every future member of the scope inherits them.

One systemd unit is written per node (patroni-<node>) instead of using the
packaged single `patroni` unit, because a host may carry several nodes.
"""

import json
import shlex
from urllib.parse import urlsplit

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "PyYAML is required to render Patroni configuration.\n"
        "  pip install -r requirements.txt"
    ) from exc

from aspects import (auth_setup, etcd_management, pg_server_management,
                     service_management)

CONFIG_DIR = "/etc/patroni"
NAMESPACE = "/service/"

# DCS timings. ttl must exceed loop_wait + retry_timeout or a healthy leader can
# lose its lease during a slow etcd round-trip and trigger a needless failover.
DEFAULT_TTL = 30
DEFAULT_LOOP_WAIT = 10
DEFAULT_RETRY_TIMEOUT = 10
MAX_LAG_ON_FAILOVER = 1048576  # 1 MB


def config_path(node_name):
    return f"{CONFIG_DIR}/{node_name}.yml"


def unit_name(node_name):
    return f"patroni-{node_name}"


def _find_binary(executor, name, hint):
    """Locate an executable on PATH, then at the usual absolute locations.

    The absolute fallbacks matter because a source build symlinks Patroni into
    /usr/local/bin, which is not always on a non-login shell's PATH.
    """
    found = executor.which(name)
    if found:
        return found
    for path in (f"/usr/bin/{name}", f"/usr/local/bin/{name}"):
        if executor.exists(path):
            return path
    raise RuntimeError(f"{executor.host}: {name} not found — {hint}")


def as_db_user(executor, user):
    """`sudo -n -u <user>`, valid even in an already-root session.

    sudo_prefix() is empty when the session is root, which is right for a plain
    privileged command but drops the -u target when the point is to step *down*
    to the postgres user.
    """
    return f"{executor.sudo_prefix() or 'sudo -n'} -u {shlex.quote(user)}"


def patroni_binary(executor, node=None):
    """Locate the patroni entry point."""
    return _find_binary(executor, "patroni", "is pgedge-patroni installed?")


def patronictl_binary(executor, node=None):
    """Locate patronictl, which every cluster query goes through."""
    return _find_binary(executor, "patronictl", "is pgedge-patroni installed?")


# ---------------------------------------------------------------------------
# Config rendering
# ---------------------------------------------------------------------------


def etcd3_settings(endpoints):
    """Turn etcd client URLs into the `etcd3` block Patroni expects.

    plan.etcd_endpoints holds full URLs because that is what curl and the
    health checks need. Patroni does not accept them: `etcd3.hosts` must be
    bare host:port, and the scheme is carried separately in `protocol`. Handing
    Patroni "http://localhost:2379" makes it build the endpoint
    "http://http://localhost:2379", which it then retries forever — the node
    starts, stays up, and never appears in the DCS.
    """
    hosts, protocol = [], "http"
    for endpoint in endpoints:
        parsed = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
        if parsed.scheme == "https":
            protocol = "https"
        netloc = parsed.netloc or parsed.path
        if ":" not in netloc:
            netloc = f"{netloc}:{etcd_management.CLIENT_PORT}"
        hosts.append(netloc)

    settings = {"hosts": hosts}
    if protocol == "https":
        settings["protocol"] = protocol
    return settings


def build_config(plan, node):
    """Render one node's Patroni configuration as a dict.

    Only scope leaders carry a `bootstrap` block. A standby joining an existing
    scope reads its parameters from the DCS, and a stale bootstrap block on a
    replica is a common source of drift between members.
    """
    is_leader = node.is_spock
    # Patroni validates connect_address and refuses loopback names: a peer
    # reading "localhost" out of the DCS would connect to itself.
    advertise = node.advertise_address or node.address

    config = {
        "scope": node.scope,
        "name": node.name,
        "namespace": NAMESPACE,
        "restapi": {
            "listen": f"0.0.0.0:{node.restapi_port}",
            "connect_address": f"{advertise}:{node.restapi_port}",
        },
        "etcd3": etcd3_settings(plan.etcd_endpoints),
    }

    if is_leader:
        parameters = pg_server_management.spock_guc_parameters(
            plan.pg_version or plan.pg_major
        )
        # Patroni renders postgresql.conf itself, so these belong in the DCS
        # parameters block rather than being appended to a file it will rewrite.
        dcs = {
            "ttl": DEFAULT_TTL,
            "loop_wait": DEFAULT_LOOP_WAIT,
            "retry_timeout": DEFAULT_RETRY_TIMEOUT,
            "maximum_lag_on_failover": MAX_LAG_ON_FAILOVER,
            "postgresql": {
                "use_pg_rewind": True,
                "use_slots": True,
                "parameters": parameters,
            },
        }
        if node.standbys:
            # Permanent physical slots keep the leader from discarding WAL a
            # standby still needs while that standby is down.
            dcs["slots"] = {
                standby: {"type": "physical"} for standby in node.standbys
            }

        config["bootstrap"] = {
            "dcs": dcs,
            "initdb": [
                {"encoding": pg_server_management.DEFAULT_ENCODING},
                "data-checksums",
                {"locale": "C"},
            ],
            "pg_hba": auth_setup.pg_hba_entries(
                plan.nodes, plan.db_user, cidrs=plan.extra_hba_cidrs
            ),
        }

    config["postgresql"] = {
        "listen": f"0.0.0.0:{node.pg_port}",
        "connect_address": f"{advertise}:{node.pg_port}",
        "data_dir": node.data_dir,
        "bin_dir": node.bin_dir,
        "pgpass": node.pgpass_file,
        "authentication": {
            "replication": {"username": plan.db_user, "password": plan.db_password},
            "superuser": {"username": plan.db_user, "password": plan.db_password},
            "rewind": {"username": plan.db_user, "password": plan.db_password},
        },
        "parameters": {
            "port": node.pg_port,
            # /tmp is included so a socket connection works even where the
            # packaged socket directory does not exist yet.
            "unix_socket_directories": "/var/run/postgresql,/tmp",
        },
        "pg_hba": auth_setup.pg_hba_entries(
            plan.nodes, plan.db_user, cidrs=plan.extra_hba_cidrs
        ),
    }

    config["tags"] = {
        "nofailover": False,
        "noloadbalance": False,
        "clonefrom": is_leader,
        "nosync": False,
    }
    return config


def render_yaml(config):
    header = "# Managed by pg-cluster-deployment — regenerated on every deploy\n"
    return header + yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


def write_config(executor, plan, node, run_logger=None):
    """Write /etc/patroni/<node>.yml and the pgpass it points at."""
    executor.run(f"mkdir -p {CONFIG_DIR}", node=node.name)
    executor.run(f"chown {plan.db_user}:{plan.db_user} {CONFIG_DIR}", node=node.name)
    executor.run(f"chmod 750 {CONFIG_DIR}", node=node.name)

    # Patroni's own data dir must exist and belong to postgres before bootstrap;
    # initdb refuses to run into a root-owned directory.
    parent = node.data_dir.rstrip("/").rsplit("/", 1)[0] or "/"
    executor.run(f"mkdir -p {shlex.quote(parent)}", node=node.name)
    executor.run(f"mkdir -p {shlex.quote(node.data_dir)}", node=node.name)
    executor.run(
        f"chown -R {plan.db_user}:{plan.db_user} {shlex.quote(parent)}",
        node=node.name,
    )
    executor.run(f"chmod 700 {shlex.quote(node.data_dir)}", node=node.name)

    auth_setup.write_patroni_pgpass(
        executor, node.pgpass_file, plan.db_user, plan.db_password, node=node.name
    )

    content = render_yaml(build_config(plan, node))
    executor.write_file(
        node.config_file, content,
        owner=plan.db_user, mode="640", node=node.name,
    )
    if run_logger:
        run_logger.node(node.name, f"patroni config written to {node.config_file}")
    return node.config_file


def check_binaries(executor, plan, node):
    """Can the database user actually run Patroni and PostgreSQL?

    Both run as that user, never as root. When the install tree is unreadable
    to it — a source build under a hardened umask, say — the unit starts, the
    exec fails, systemd restarts it, and nothing ever reaches the DCS. Asking
    directly costs one command and names the file. Returns a list of problems.
    """
    problems = []
    checks = [("patroni", None)]
    if node.bin_dir:
        checks.append(("postgres", f"{node.bin_dir}/postgres"))

    for label, path in checks:
        if path is None:
            try:
                path = patroni_binary(executor, node=node.name)
            except RuntimeError as exc:
                problems.append(str(exc))
                continue
        ok, output = executor.try_run(
            f"{shlex.quote(path)} --version", user=plan.db_user, node=node.name
        )
        if not ok:
            detail = (output or "").strip().splitlines()
            problems.append(
                f"{plan.db_user} cannot run {path}"
                + (f": {detail[-1].strip()}" if detail else "")
            )
    return problems


def validate_config(executor, plan, node):
    """Run `patroni --validate-config` on a written config.

    Advisory only: it catches a malformed DCS block or an unreadable data
    directory in a second, where the same mistake otherwise shows up as a node
    that starts and then never registers. Returns a message, or "" when the
    config validates (or the check itself could not run).
    """
    try:
        binary = patroni_binary(executor, node=node.name)
    except RuntimeError:
        return ""

    # user= lets the executor build the step-down invocation itself.
    ok, output = executor.try_run(
        f"{binary} --validate-config {shlex.quote(node.config_file)} 2>&1",
        user=plan.db_user, node=node.name,
    )
    if ok:
        return ""
    return output.strip() or "patroni --validate-config reported a problem"


def write_service_unit(executor, plan, node):
    """Install a per-node systemd unit for Patroni."""
    binary = patroni_binary(executor, node=node.name)
    content = f"""# Managed by pg-cluster-deployment
[Unit]
Description=Patroni for PostgreSQL node {node.name} (scope {node.scope})
Documentation=https://patroni.readthedocs.io/
After=network-online.target etcd.service
Wants=network-online.target

[Service]
Type=simple
User={plan.db_user}
Group={plan.db_user}
# Patroni forks multiprocessing workers that inherit this cwd; it must be a
# directory the postgres user can read, or the workers die on startup.
WorkingDirectory=/tmp
Environment=PATH={node.bin_dir}:/usr/local/bin:/usr/bin:/bin
Environment=PGPASSFILE={node.pgpass_file}
ExecStart={binary} {node.config_file}
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=process
KillSignal=SIGINT
TimeoutStartSec=300
TimeoutStopSec=60
Restart=on-failure
RestartSec=10
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""
    return service_management.write_unit(executor, unit_name(node.name), content,
                                         node=node.name)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def port_conflict(executor, node):
    """Report anything already listening on this node's ports.

    Patroni fails the same way whether its REST port or its PostgreSQL port is
    taken: it starts, cannot bind, and never registers in the DCS. Saying so
    before the wait begins turns a silent timeout into an obvious cause.
    Returns a message, or "" when both ports are free.
    """
    if is_running(executor, node)[0]:
        return ""  # this node's own Patroni — a restart, not a conflict

    busy = []
    for label, port in (("patroni REST", node.restapi_port),
                        ("postgresql", node.pg_port)):
        ok, output = executor.try_run(
            f"(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) "
            f"| grep -E '[:.]{port}[[:space:]]' | head -n 1",
            node=node.name,
        )
        if ok and output.strip():
            busy.append(f"{label} port {port}")

    if not busy:
        return ""
    return (
        f"{', '.join(busy)} on {node.host} already has a listener, and it is "
        f"not this node's Patroni. Patroni cannot bind it and will never reach "
        f"the DCS — stop whatever holds the port, or deploy with a different "
        f"--base-port / --base-restapi-port."
    )


def start(executor, plan, node, run_logger=None):
    """Start a node's Patroni instance, with a no-systemd fallback."""
    unit = unit_name(node.name)

    if service_management.has_systemd(executor, node=node.name):
        ok, output = service_management.start(executor, unit, node=node.name)
        if ok:
            return f"{unit} started via systemd"
        detail = service_management.journal(executor, unit, node=node.name)
        if run_logger:
            run_logger.warn(
                f"{node.name}: systemd start failed, falling back to a direct "
                f"launch\n{output.strip()}\n{detail}"
            )

    binary = patroni_binary(executor, node=node.name)
    log_path = f"/tmp/patroni_{node.name}.log"
    executor.try_run(
        f"nohup {as_db_user(executor, plan.db_user)} bash -c "
        f"'cd /tmp && exec {binary} {node.config_file}' "
        f"> {log_path} 2>&1 </dev/null &",
        node=node.name,
    )
    return f"patroni started directly (log {log_path})"


def restart_count(executor, node_name, node=None):
    """How many times systemd has restarted this node's Patroni unit."""
    _, output = executor.try_run(
        f"systemctl show -p NRestarts --value {shlex.quote(unit_name(node_name))} "
        f"2>/dev/null",
        node=node or node_name,
    )
    text = (output or "").strip().splitlines()
    try:
        return int(text[-1]) if text else 0
    except ValueError:
        return 0


def installed_units(executor, node=None):
    """Every patroni-<node>.service unit installed on a host."""
    _, output = executor.try_run(
        "systemctl list-unit-files 'patroni-*.service' --no-legend 2>/dev/null "
        "| awk '{print $1}'",
        node=node,
    )
    return sorted(
        line.strip() for line in output.splitlines()
        if line.strip().startswith("patroni-")
    )


def unit_node_name(unit):
    """'patroni-n1s1.service' -> 'n1s1'."""
    name = unit.strip()
    if name.endswith(".service"):
        name = name[: -len(".service")]
    return name[len("patroni-"):] if name.startswith("patroni-") else name


def remove_instance(executor, node_name, data_root="", node=None):
    """Delete one Patroni instance: unit, config, pgpass and data directory.

    Used against instances no longer in the plan. A leftover instance is not
    idle — it holds its scope's leader lock in the DCS, so a fresh node with
    the same scope joins it as a replica and never becomes the leader the
    deployment is waiting for.
    """
    unit = unit_name(node_name)
    stop(executor, node_name)
    executor.try_run(f"systemctl disable {shlex.quote(unit)} 2>/dev/null || true",
                     node=node)
    executor.try_run(f"rm -f /etc/systemd/system/{unit}.service", node=node)
    executor.try_run(f"rm -f {config_path(node_name)}", node=node)
    executor.try_run(f"rm -f /tmp/patroni_{node_name}.log", node=node)
    if data_root:
        executor.try_run(
            f"rm -rf {shlex.quote(data_root.rstrip('/'))}/{node_name}", node=node
        )
    executor.try_run("systemctl daemon-reload 2>/dev/null || true", node=node)
    return unit


def stop(executor, node_name):
    """Stop a node's Patroni instance without triggering a failover storm."""
    service_management.stop(executor, unit_name(node_name), node=node_name)
    executor.try_run(
        f"pkill -f 'patroni {config_path(node_name)}' 2>/dev/null || true",
        node=node_name,
    )


def logs(executor, node_name, lines=60):
    """Patroni's recent output, wherever it landed."""
    text = service_management.journal(executor, unit_name(node_name), lines=lines,
                                      node=node_name)
    if text and "No entries" not in text:
        return text
    _, output = executor.try_run(
        f"tail -n {lines} /tmp/patroni_{node_name}.log 2>/dev/null || true",
        node=node_name,
    )
    return output.strip()


def is_running(executor, node, systemd=None):
    """Is this node's Patroni process actually alive? Returns (alive, detail).

    Waiting is only worth doing while there is something left to wait for. A
    Patroni that exited, or that systemd is restarting in a loop, will never
    appear in the DCS no matter how long the caller blocks.
    """
    unit = unit_name(node.name)
    if systemd is None:
        systemd = service_management.has_systemd(executor, node=node.name)

    if systemd:
        _, output = executor.try_run(
            f"systemctl is-active {shlex.quote(unit)} 2>/dev/null || true",
            node=node.name,
        )
        state = (output.strip().splitlines() or ["unknown"])[-1].strip()
        if state in ("active", "activating", "reloading"):
            return True, f"{unit} is {state}"
        if state == "failed":
            return False, f"{unit} has failed"
        # 'inactive' right after a crash, or while systemd waits out RestartSec.
        return False, f"{unit} is {state or 'unknown'}"

    ok, _ = executor.try_run(
        f"pgrep -f {shlex.quote('patroni ' + node.config_file)} >/dev/null 2>&1",
        node=node.name,
    )
    return ok, "patroni process running" if ok else "no patroni process is running"


def _dcs_error(executor, node):
    """patronictl's own complaint, for when it returns nothing usable.

    list_members() swallows errors by design — every caller polls it — so the
    diagnosis has to be asked for separately.
    """
    try:
        binary = patronictl_binary(executor, node=node.name)
    except RuntimeError as exc:
        return str(exc)
    _, output = executor.try_run(
        f"timeout 20 {binary} -c {node.config_file} list 2>&1 | tail -n 5",
        node=node.name,
    )
    return " / ".join(line.strip() for line in output.strip().splitlines() if line.strip())


def wait_for_role(executor, node, expected_roles, timeout=300, interval=5,
                  run_logger=None):
    """Block until a node reports one of `expected_roles` to the DCS.

    Roles come straight from patronictl: 'Leader', 'Replica', 'Sync Standby'.
    While the node stays invisible the wait keeps checking that Patroni is
    still running and asks its REST API directly, so a dead process or an
    unreachable DCS is reported within seconds instead of after the full
    timeout. Returns (ok, role, detail).
    """
    wanted = {role.lower() for role in expected_roles}
    leader_wanted = bool(wanted & {"leader", "master", "primary"})
    attempts = max(1, timeout // interval)
    systemd = service_management.has_systemd(executor, node=node.name)
    last_detail = ""
    usurped = 0
    # "activating" looks alive, and a unit that exits and is restarted every
    # RestartSec spends much of its time in exactly that state. The restart
    # counter is what tells the two apart.
    restarts_at_start = restart_count(executor, node.name) if systemd else 0

    for attempt in range(1, attempts + 1):
        members = list_members(executor, node, node_name=node.name)
        visible = False
        for member in members:
            if member.get("name") != node.name:
                continue
            visible = True
            role = (member.get("role") or "").lower()
            state = (member.get("state") or "").lower()
            if role in wanted and state in ("running", "streaming", "in archive recovery"):
                return True, member.get("role"), f"state={member.get('state')}"
            last_detail = f"role={member.get('role')} state={member.get('state')}"

        # A node that should lead its scope but is following someone else will
        # not change its mind: Patroni only promotes it if the lock owner goes
        # away. Waiting out the timeout just delays the same failure.
        if leader_wanted and visible:
            owner = next(
                (m.get("name") for m in members
                 if (m.get("role") or "").lower() in ("leader", "master", "primary")
                 and m.get("name") != node.name),
                None,
            )
            if owner:
                usurped += 1
                last_detail = (
                    f"scope {node.scope} is already led by {owner}, so {node.name} "
                    f"joined it as a replica. {owner} is not part of this "
                    f"deployment — remove it (./pg_deploy_cluster.sh --cleanup) "
                    f"or redeploy with --clean"
                )
                if usurped * interval >= 30:
                    return False, None, last_detail
            else:
                usurped = 0

        if not visible:
            # Check liveness every third poll: often enough to fail fast,
            # rarely enough not to triple the traffic to the host.
            if attempt == 1 or attempt % 3 == 0:
                alive, service_detail = is_running(executor, node, systemd=systemd)
                if not alive:
                    return False, None, (
                        f"patroni is not running on {node.host} — {service_detail}"
                    )
                if systemd:
                    restarts = restart_count(executor, node.name) - restarts_at_start
                    if restarts >= 3:
                        return False, None, (
                            f"{unit_name(node.name)} has restarted {restarts} times "
                            f"without registering — it is failing at startup. "
                            f"Check: journalctl -u {unit_name(node.name)} -n 50"
                        )
                rest = rest_health(executor, node, node_name=node.name)
                if rest:
                    last_detail = (
                        f"patroni is up on port {node.restapi_port} "
                        f"(state={rest.get('state')}, role={rest.get('role')}) "
                        f"but has not reached the DCS"
                    )
                else:
                    last_detail = (
                        _dcs_error(executor, node)
                        or f"no answer from patroni on port {node.restapi_port}"
                    )

        if run_logger and attempt % 6 == 0:
            run_logger.info(
                f"    waiting for {node.name} to reach "
                f"{'/'.join(expected_roles)} ({attempt * interval}s) — "
                f"{last_detail or 'not yet visible in the DCS'}"
            )
        executor.try_run(f"sleep {interval}", node=node.name)

    return False, None, last_detail or "node never appeared in patronictl output"


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def list_members(executor, node, node_name=None):
    """Members of a node's scope, normalised to lowercase keys.

    Uses patronictl's JSON output; the table format changes between Patroni
    releases and is not worth parsing.
    """
    try:
        binary = patronictl_binary(executor, node=node_name)
    except RuntimeError:
        return []

    ok, output = executor.try_run(
        f"timeout 20 {binary} -c {node.config_file} list -f json 2>/dev/null",
        node=node_name,
    )
    if not ok or not output.strip():
        return []

    try:
        raw = json.loads(output.strip())
    except json.JSONDecodeError:
        return []

    members = []
    for entry in raw if isinstance(raw, list) else []:
        members.append(
            {
                "name": entry.get("Member") or entry.get("member"),
                "host": entry.get("Host") or entry.get("host"),
                "role": entry.get("Role") or entry.get("role"),
                "state": entry.get("State") or entry.get("state"),
                "timeline": entry.get("TL") or entry.get("tl"),
                "lag_mb": entry.get("Lag in MB", entry.get("lag_in_mb")),
                "cluster": entry.get("Cluster") or node.scope,
            }
        )
    return members


def cluster_status(executor, node, node_name=None):
    """One scope's state, shaped for the dashboard."""
    members = list_members(executor, node, node_name=node_name)
    leader = next(
        (m for m in members if (m.get("role") or "").lower() == "leader"), None
    )
    return {
        "scope": node.scope,
        "members": members,
        "leader": leader.get("name") if leader else None,
        "leader_host": leader.get("host") if leader else None,
        "member_count": len(members),
        "healthy": bool(leader) and all(
            (m.get("state") or "").lower() in ("running", "streaming")
            for m in members
        ),
    }


def rest_health(executor, node, node_name=None):
    """Ask a node's REST API directly — works even when the DCS is unreachable."""
    ok, output = executor.try_run(
        f"curl -fsS --max-time 5 http://127.0.0.1:{node.restapi_port}/patroni "
        f"2>/dev/null",
        node=node_name,
    )
    if not ok or not output.strip():
        return None
    try:
        return json.loads(output.strip())
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def switchover(executor, node, candidate, node_name=None):
    """Planned role change within a scope (no data loss)."""
    binary = patronictl_binary(executor, node=node_name)
    return executor.try_run(
        f"{binary} -c {node.config_file} switchover {node.scope} "
        f"--candidate {candidate} --force",
        node=node_name,
    )


def failover(executor, node, candidate, node_name=None):
    """Force a role change even when the current leader looks healthy."""
    binary = patronictl_binary(executor, node=node_name)
    return executor.try_run(
        f"{binary} -c {node.config_file} failover {node.scope} "
        f"--candidate {candidate} --force",
        node=node_name,
    )


def reinitialise(executor, node, member, node_name=None):
    """Rebuild a member from the current leader."""
    binary = patronictl_binary(executor, node=node_name)
    return executor.try_run(
        f"{binary} -c {node.config_file} reinit {node.scope} {member} --force",
        node=node_name,
    )


def remove_scope(executor, node, node_name=None):
    """Delete a scope's keys from the DCS.

    A redeploy that reuses a scope name inherits the old cluster's leader key
    and system identifier, and every new member then refuses to start.
    """
    binary = patronictl_binary(executor, node=node_name)
    return executor.try_run(
        f"printf '%s\\nYes I am aware\\n' {shlex.quote(node.scope)} | "
        f"{binary} -c {node.config_file} remove {node.scope} 2>&1 || true",
        node=node_name,
    )
