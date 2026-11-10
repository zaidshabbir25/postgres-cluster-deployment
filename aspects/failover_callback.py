#!/usr/bin/env python3
"""Retarget a Spock node's peers the moment Patroni promotes its standby.

Patroni moves a leader inside a scope; Spock's peers hold the DSN they were
given when the mesh was cross-wired, so they carry on replicating from an
address that is now a read-only replica. `--repair-failover` fixes that by
hand. This module is the same repair, triggered by Patroni itself.

The hook is Patroni's `on_role_change` callback: it runs on the member whose
role changed, with the new role as an argument, so the promoted standby runs
it the instant it becomes primary. Patroni does not wait for it and ignores
its exit status, which is why the script logs rather than reports.

Two decisions worth knowing about:

**It runs on the node, not from the control machine.** The machine that ran
the deployment may be a laptop that is closed when the failover happens. A
cluster host, on the other hand, can always reach its peers — they are in the
same mesh. So the script is self-contained: a shell file on each member, with
no Python, no repository checkout and no SSH.

**It discovers its peers at run time** rather than carrying a baked-in list.
The promoted node's own subscriptions already name every peer and hold a
current DSN for each, so `spock.sub_show_status()` is the source. A cluster
that gains a node later needs no regenerated scripts — that list is derived,
not remembered.

The repair itself is idempotent, which matters here: a scope that flaps fires
this callback repeatedly, and re-running it on peers that are already correct
has to be harmless.
"""

import shlex

CONFIG_DIR = "/etc/patroni"

#: Patroni has spelled the promoted role both ways across versions, and a
#: standby_leader is not a promotion to a writable primary.
PRIMARY_ROLES = ("master", "primary")


def script_path(node_name):
    """Where this node's callback lives."""
    return f"{CONFIG_DIR}/on-role-change-{node_name}.sh"


def log_path(node_name):
    return f"/var/log/pgedge/{node_name}-role-change.log"


def render(plan, node, spock_name=None):
    """The callback script for one member.

    `spock_name` is the Spock node whose identity this member serves — its own
    name for a leader, its leader's name for a standby. That is the name the
    peers' subscriptions are built from, and it does not change when the scope
    fails over, which is the whole point.
    """
    spock_name = spock_name or node.name
    address = node.advertise_address or node.address
    psql = f"{node.bin_dir}/psql" if node.bin_dir else "psql"
    interface = f"{spock_name}_promoted_{node.pg_port}"
    local_dsn = (
        f"host={address} port={node.pg_port} dbname={plan.db_name} "
        f"user={plan.db_user} password={plan.db_password}"
    )
    new_dsn = local_dsn

    return f"""#!/bin/bash
# Managed by pg-cluster-deployment — regenerated on every deploy.
#
# Patroni calls this as: <script> <action> <role> <scope>
# On promotion, point every Spock peer at this instance, because Patroni has
# moved the leader of {node.scope} and Spock has no way to notice.
set -u

action="${{1:-}}"
role="${{2:-}}"
scope="${{3:-}}"

# Overridable so the script can be run by hand, or tested, without writing
# into /var/log.
log="${{PGEDGE_ROLE_CHANGE_LOG:-{log_path(node.name)}}}"
mkdir -p "$(dirname "$log")" 2>/dev/null || true
exec >>"$log" 2>&1
echo "=== $(date -Is) $action role=$role scope=$scope"

case "$role" in
  {' | '.join(PRIMARY_ROLES)}) ;;
  *) echo "not a promotion; nothing to do"; exit 0 ;;
esac

PSQL={shlex.quote(psql)}
NODE={shlex.quote(spock_name)}
IFACE={shlex.quote(interface)}
NEW_DSN={shlex.quote(new_dsn)}
LOCAL_DSN={shlex.quote(local_dsn)}

# The peers and their current DSNs come from this node's own subscriptions, so
# a cluster that has grown since deployment needs no new script.
peers=$("$PSQL" -At -d "$LOCAL_DSN" \\
  -c "SELECT provider_node || '|' || provider_dsn FROM spock.sub_show_status()" \\
  2>/dev/null)

if [ -z "$peers" ]; then
  echo "no Spock subscriptions here yet; nothing to retarget"
  exit 0
fi

rc=0
while IFS='|' read -r peer peer_dsn; do
  [ -n "$peer" ] || continue
  sub="sub_${{NODE}}_${{peer}}"
  echo "-- $peer: pointing $sub at $IFACE"

  run_sql() {{
    # Errors are printed, not fatal: one unreachable peer must not stop the
    # others being repaired.
    "$PSQL" -At -d "$peer_dsn" -c "$1" 2>&1 || return 1
  }}

  run_sql "SELECT spock.sub_disable('$sub', true);" || rc=1
  # 'already exists' is the expected answer when this scope has failed over to
  # this instance before. The switch below is what matters.
  out=$("$PSQL" -At -d "$peer_dsn" \\
        -c "SELECT spock.node_add_interface('$NODE', '$IFACE', '$NEW_DSN');" 2>&1) \\
    || case "$out" in
         *"already exists"*) echo "   interface already present" ;;
         *) echo "   add interface failed: $out"; rc=1 ;;
       esac
  run_sql "SELECT spock.sub_alter_interface('$sub', '$IFACE');" || rc=1
  # immediate := true restarts the apply worker now; without it the peer keeps
  # reporting 'down' until its next cycle.
  run_sql "SELECT spock.sub_enable('$sub', true);" || rc=1
done <<EOF
$peers
EOF

echo "=== done (rc=$rc)"
# Patroni ignores the exit status; the log is the record.
exit 0
"""


def install(executor, plan, node, spock_name=None, run_logger=None):
    """Write the callback onto a node and make it executable."""
    path = script_path(node.name)
    executor.write_file(
        path, render(plan, node, spock_name=spock_name),
        owner=plan.db_user, mode="750", node=node.name,
    )
    executor.try_run(
        f"mkdir -p /var/log/pgedge && "
        f"chown {plan.db_user}:{plan.db_user} /var/log/pgedge",
        node=node.name,
    )
    if run_logger:
        run_logger.node(node.name, f"failover callback written to {path}")
    return path


def remove(executor, node):
    """Take the callback off a node."""
    path = script_path(node.name)
    executor.try_run(f"rm -f {shlex.quote(path)}", node=node.name)
    return path


def spock_name_for(plan, node):
    """The Spock identity a member serves: its own, or its leader's."""
    if node.is_spock:
        return node.name
    return node.leader or node.name
