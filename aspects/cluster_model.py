#!/usr/bin/env python3
"""The data model every layer agrees on.

A ClusterPlan is the single description of what is being deployed: which hosts
exist, which nodes land on them, which ports they use, who stands by for whom,
and where etcd lives. Deployment writes it, reports read it, and the dashboard
polls against it — so it serialises cleanly to JSON and back.
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# Roles a node can hold.
ROLE_SPOCK = "spock"      # a multi-master Spock node, cross-wired to its peers
ROLE_STANDBY = "standby"  # a Patroni physical standby of one Spock node


@dataclass
class Host:
    """A machine we can run commands on."""

    name: str
    address: str
    # What peers should dial. Equal to address unless that is a loopback name,
    # which Patroni rejects in connect_address.
    advertise_address: str = ""
    username: str = "root"
    key_file: Optional[str] = None
    port: int = 22
    local: bool = False
    description: str = ""

    # Filled in during deployment once the host has been probed.
    family: str = ""          # 'rhel' | 'deb'
    platform: Dict[str, Any] = field(default_factory=dict)
    bin_dir: str = ""
    is_etcd_member: bool = False

    def to_dict(self):
        payload = asdict(self)
        payload.pop("key_file", None)  # never persist key paths into reports
        return payload


@dataclass
class Node:
    """One PostgreSQL instance in the cluster."""

    name: str
    role: str
    host: str                 # Host.name this node runs on
    address: str              # address clients and peers connect to
    pg_port: int
    restapi_port: int
    data_dir: str
    scope: str                # Patroni scope; standbys share their leader's
    config_file: str
    pgpass_file: str
    # Routable form of `address` for Patroni's connect_address, which rejects
    # loopback names. Empty means `address` is already routable.
    advertise_address: str = ""
    # Set only when this node's Spock differs from the cluster's; empty means
    # it follows ClusterPlan.spock_major.
    spock_major: str = ""
    # Per-scope replication mode, for a leader whose scope differs from the
    # cluster default. Empty means it follows ClusterPlan.synchronous_mode.
    synchronous_mode: str = ""
    synchronous_node_count: int = 0
    leader: Optional[str] = None   # for standbys: the Spock node they follow
    standbys: List[str] = field(default_factory=list)  # for Spock nodes

    # Filled in during deployment.
    family: str = ""
    bin_dir: str = ""
    pg_version: str = ""

    @property
    def is_spock(self):
        return self.role == ROLE_SPOCK

    @property
    def is_standby(self):
        return self.role == ROLE_STANDBY

    def dsn(self, db_name, db_user, db_password=None, host=None):
        """Build a libpq DSN for this node.

        The password is included when given because zodan's procedures pass the
        DSN through dblink from inside the server, where .pgpass on the client
        side is not in play.
        """
        parts = [
            f"host={host or self.address}",
            f"port={self.pg_port}",
            f"dbname={db_name}",
            f"user={db_user}",
        ]
        if db_password:
            parts.append(f"password={db_password}")
        return " ".join(parts)

    def to_dict(self):
        return asdict(self)


@dataclass
class ClusterPlan:
    """Everything needed to deploy, report on, or monitor one cluster."""

    cluster_name: str
    db_name: str = "postgres"
    db_user: str = "postgres"
    db_password: str = "postgres"

    pg_major: str = "17"
    pg_version: str = ""           # discovered at deploy time (e.g. 17.11)
    spock_major: str = "50"
    repo_channel: str = "release"
    deploy_mode: str = "packages"  # 'packages' | 'source'

    node_count: int = 2
    standby_of: List[str] = field(default_factory=list)  # Spock nodes to shadow

    base_pg_port: int = 5432
    base_restapi_port: int = 8008
    data_root: str = "/var/lib/pgedge"

    zodan_sql: str = ""

    # Replication mode for the Patroni scopes, as Patroni names it:
    # "off" (asynchronous), "on" (synchronous) or "quorum". Patroni owns
    # synchronous_standby_names; these settings live in the DCS, so changing
    # them on a running cluster means patronictl edit-config, not a file.
    # Days of PostgreSQL server log to keep under <data_dir>/log. 7 rotates
    # one file per weekday, which is what most people want and needs no cron.
    log_retention_days: int = 7

    synchronous_mode: str = "off"
    synchronous_node_count: int = 1
    synchronous_mode_strict: bool = False
    etcd_endpoints: List[str] = field(default_factory=list)
    extra_hba_cidrs: List[str] = field(default_factory=list)

    hosts: List[Host] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)

    source_build: Dict[str, Any] = field(default_factory=dict)
    run_id: str = ""

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def host(self, name):
        for host in self.hosts:
            if host.name == name:
                return host
        raise KeyError(f"unknown host {name!r}")

    def node(self, name):
        for node in self.nodes:
            if node.name == name:
                return node
        raise KeyError(f"unknown node {name!r}")

    @property
    def spock_nodes(self):
        return [n for n in self.nodes if n.is_spock]

    @property
    def standby_nodes(self):
        return [n for n in self.nodes if n.is_standby]

    @property
    def etcd_hosts(self):
        return [h for h in self.hosts if h.is_etcd_member]

    def nodes_on(self, host_name):
        return [n for n in self.nodes if n.host == host_name]

    def scopes(self):
        """Patroni scopes in the cluster, each mapped to its members."""
        grouped = {}
        for node in self.nodes:
            grouped.setdefault(node.scope, []).append(node)
        return grouped

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self):
        payload = asdict(self)
        payload["hosts"] = [h.to_dict() for h in self.hosts]
        payload["nodes"] = [n.to_dict() for n in self.nodes]
        # The password is a deployment input, not something to persist into a
        # report or state file that gets shared around.
        payload["db_password"] = "***"
        return payload

    @classmethod
    def from_dict(cls, payload, db_password=None):
        data = dict(payload)
        hosts = [Host(**h) for h in data.pop("hosts", [])]
        nodes = [Node(**n) for n in data.pop("nodes", [])]
        if db_password is not None:
            data["db_password"] = db_password
        elif data.get("db_password") == "***":
            data["db_password"] = ""
        plan = cls(**data)
        plan.hosts = hosts
        plan.nodes = nodes
        return plan

    def summary_lines(self):
        """Human-readable plan, printed before the deployment starts."""
        lines = [
            f"Cluster        : {self.cluster_name}",
            f"Deployment     : {self.deploy_mode}"
            + (f" (channel {self.repo_channel})" if self.deploy_mode == "packages" else ""),
            f"PostgreSQL     : {self.pg_major}"
            + (f" ({self.pg_version})" if self.pg_version else ""),
            f"Spock          : spock{self.spock_major}",
            f"Database       : {self.db_name} as {self.db_user}",
            f"Hosts          : {len(self.hosts)}",
            f"Spock nodes    : {len(self.spock_nodes)}",
            f"Standby nodes  : {len(self.standby_nodes)}",
            f"etcd           : {', '.join(self.etcd_endpoints) or 'not planned yet'}",
            "",
            f"{'NODE':<10} {'ROLE':<9} {'HOST':<16} {'ADDRESS':<16} "
            f"{'PG':<7} {'API':<6} {'SCOPE':<18} FOLLOWS",
        ]
        for node in self.nodes:
            lines.append(
                f"{node.name:<10} {node.role:<9} {node.host:<16} {node.address:<16} "
                f"{node.pg_port:<7} {node.restapi_port:<6} {node.scope:<18} "
                f"{node.leader or '-'}"
            )
        return lines
