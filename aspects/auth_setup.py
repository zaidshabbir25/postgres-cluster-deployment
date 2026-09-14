#!/usr/bin/env python3
"""Password-free psql access across the cluster.

The goal is that `psql -h <any node> -p <port> -U postgres` and
`psql service=n2` both work from any host without ever typing a password,
while the wire still uses scram-sha-256 rather than trust.

Three pieces do that:
  * .pgpass in the postgres user's home — one entry per node in the cluster
  * pg_service.conf — named connections, so scripts say `service=n2`
  * pg_hba entries — generated here, applied by Patroni (which owns pg_hba.conf
    for the clusters it bootstraps) or written directly for standalone nodes

.pgpass carries every node, not just the local one, because zodan cross-wiring
and the health collector both connect outward from whichever node they run on.
"""

import ipaddress
import shlex

from aspects import platform_detect

PGPASS_NAME = ".pgpass"
PGSERVICE_NAME = ".pg_service.conf"
PROFILE_SCRIPT = "/etc/profile.d/pgcluster.sh"


def pgpass_content(nodes, db_user, db_password):
    """Build .pgpass covering every node, by address and by loopback.

    Format: hostname:port:database:username:password
    """
    lines = [
        "# Managed by pg-cluster-deployment — do not edit by hand",
        f"*:*:*:{db_user}:{db_password}",
    ]
    for node in nodes:
        lines.append(f"{node.address}:{node.pg_port}:*:{db_user}:{db_password}")
        lines.append(f"127.0.0.1:{node.pg_port}:*:{db_user}:{db_password}")
    # Deduplicate while keeping order stable so the file does not churn.
    seen, unique = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return "\n".join(unique) + "\n"


def pgservice_content(nodes, db_user, db_name):
    """Build pg_service.conf so any node is reachable as `service=<name>`."""
    blocks = ["# Managed by pg-cluster-deployment", ""]
    for node in nodes:
        blocks += [
            f"[{node.name}]",
            f"host={node.address}",
            f"port={node.pg_port}",
            f"dbname={db_name}",
            f"user={db_user}",
            "",
        ]
    return "\n".join(blocks)


def hba_target(address):
    """Render one address as a pg_hba target, or "" if it needs no rule.

    An IP literal takes a mask; a host name must not have one — "localhost/32"
    is a syntax error that stops PostgreSQL from starting, and Patroni owns
    pg_hba.conf for the clusters it bootstraps, so the node simply never comes
    up. Loopback is skipped because the fixed rules above already cover it.
    """
    address = (address or "").strip()
    if not address or platform_detect.is_loopback(address):
        return ""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address  # a host name: pg_hba matches it by reverse lookup
    return f"{address}/{32 if parsed.version == 4 else 128}"


def pg_hba_entries(nodes, db_user, cidrs=None, method="scram-sha-256"):
    """pg_hba rules that let the cluster talk to itself.

    Every node address is listed explicitly rather than opening a wide CIDR, so
    the rule set stays as narrow as the cluster actually is. Extra CIDRs (an
    application subnet, a bastion) can be added by the caller.
    """
    entries = [
        "local all all trust",
        f"host all all 127.0.0.1/32 {method}",
        f"host all all ::1/128 {method}",
        f"host replication {db_user} 127.0.0.1/32 {method}",
        f"host replication {db_user} ::1/128 {method}",
    ]
    # Both forms matter: clients connect to the listed address, while Patroni
    # replication connects to the address the leader advertises.
    targets = set()
    for node in nodes:
        targets.add(hba_target(node.address))
        targets.add(hba_target(getattr(node, "advertise_address", "")))
    for address in sorted(t for t in targets if t):
        entries.append(f"host all all {address} {method}")
        entries.append(f"host replication {db_user} {address} {method}")
    for cidr in cidrs or []:
        entries.append(f"host all all {cidr} {method}")
        entries.append(f"host replication {db_user} {cidr} {method}")
    return entries


def configure_host(executor, family, nodes, db_user, db_password, db_name,
                   pg_bin_dir, node=None):
    """Install .pgpass, pg_service.conf and a PATH/PGPASSFILE profile on a host.

    Written for both the postgres user (Patroni and psql-as-postgres) and root
    (operators who ssh in and run psql without switching user).
    """
    home = platform_detect.pg_home(family)
    pgpass = pgpass_content(nodes, db_user, db_password)
    pgservice = pgservice_content(nodes, db_user, db_name)

    # The postgres user's home may not exist yet on a host where the server
    # package created the account without it.
    executor.run(f"mkdir -p {shlex.quote(home)}", node=node)
    executor.run(f"chown {db_user}:{db_user} {shlex.quote(home)}", node=node)

    for owner, base in ((db_user, home), ("root", "/root")):
        executor.write_file(f"{base}/{PGPASS_NAME}", pgpass, owner=owner,
                            mode="600", node=node)
        executor.write_file(f"{base}/{PGSERVICE_NAME}", pgservice, owner=owner,
                            mode="644", node=node)

    profile = (
        "# Managed by pg-cluster-deployment\n"
        f"export PATH={pg_bin_dir}:$PATH\n"
        f"export PGPASSFILE=${{HOME}}/{PGPASS_NAME}\n"
        f"export PGSERVICEFILE=${{HOME}}/{PGSERVICE_NAME}\n"
        f"export PGUSER={db_user}\n"
        f"export PGDATABASE={db_name}\n"
    )
    executor.write_file(PROFILE_SCRIPT, profile, owner="root", mode="644", node=node)

    return f"passwordless psql configured for {len(nodes)} node(s)"


def write_patroni_pgpass(executor, path, db_user, db_password, node=None):
    """Per-node pgpass that Patroni points at for replication connections.

    Patroni requires this file to be 0600 and owned by the user it runs as, and
    rewrites it on every reload — so it is kept separate from the operator
    .pgpass rather than shared.
    """
    executor.write_file(
        path, f"*:*:*:{db_user}:{db_password}\n",
        owner=db_user, mode="600", node=node,
    )
    return path


def set_superuser_password(executor, bin_dir, port, db_user, db_password, node=None):
    """Set the superuser password on a running node.

    Needed on clusters Patroni did not bootstrap (source builds, adopted
    clusters); Patroni sets it itself during bootstrap.
    """
    from aspects import pg_server_management

    sql = f"ALTER USER {db_user} WITH PASSWORD {_sql_literal(db_password)};"
    return pg_server_management.psql(
        executor, bin_dir, port, db_user, sql, node=node, check=False
    )


def _sql_literal(value):
    """Quote a value as a SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"
