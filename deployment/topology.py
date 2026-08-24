#!/usr/bin/env python3
"""Turn an inventory plus a node count into a concrete cluster layout.

Placement rules, in priority order:

1. One Spock node per host. Spock nodes are the multi-master participants; two
   of them on one machine share a failure domain and buy nothing.
2. When nodes outnumber hosts, pack the surplus onto the least-loaded hosts and
   give them distinct ports (5432, 5433, ...). This is what makes a one-VM
   demo possible without a separate code path.
3. A standby always goes on a *different* host from the node it follows, if
   there is one. A standby sharing its leader's machine cannot survive the
   failure it exists to survive — so when that is unavoidable the plan says so
   out loud rather than quietly pretending to be highly available.
"""

from aspects import etcd_management, platform_detect
from aspects.cluster_model import ClusterPlan, Node, ROLE_SPOCK, ROLE_STANDBY

DEFAULT_DATA_ROOT = "/var/lib/pgedge"


class TopologyError(ValueError):
    pass


def node_names(count):
    """n1, n2, ... — the naming the zodan procedures and Patroni scopes use."""
    return [f"n{index}" for index in range(1, count + 1)]


def standby_name(leader_name, ordinal=1):
    return f"{leader_name}s{ordinal}"


def plan_cluster(hosts, cluster_name, node_count, standby_of=None,
                 db_name="postgres", db_user="postgres", db_password="postgres",
                 pg_major="17", pg_version="", spock_major="50",
                 repo_channel="release", deploy_mode="packages",
                 base_pg_port=5432, base_restapi_port=8008,
                 data_root=DEFAULT_DATA_ROOT, extra_hba_cidrs=None,
                 source_build=None, zodan_sql="", run_id=""):
    """Build the ClusterPlan. Returns (plan, warnings)."""
    if not hosts:
        raise TopologyError("no hosts available — check the inventory")
    if node_count < 1:
        raise TopologyError(f"node count must be at least 1, got {node_count}")

    warnings = []
    names = node_names(node_count)

    standby_of = [s.strip() for s in (standby_of or []) if s.strip()]
    unknown = [name for name in standby_of if name not in names]
    if unknown:
        raise TopologyError(
            f"cannot add a standby for unknown node(s) {', '.join(unknown)}; "
            f"this cluster has {', '.join(names)}"
        )

    # --- assign hosts -------------------------------------------------
    load = {host.name: 0 for host in hosts}
    assignment = {}  # node name -> host name

    for index, name in enumerate(names):
        host = hosts[index % len(hosts)]
        assignment[name] = host.name
        load[host.name] += 1

    if node_count > len(hosts):
        warnings.append(
            f"{node_count} Spock nodes across {len(hosts)} host(s): some hosts "
            f"carry several nodes on separate ports, so they share a failure "
            f"domain. Add hosts to the inventory for a fully distributed cluster."
        )

    standby_entries = []
    for leader in standby_of:
        leader_host = assignment[leader]
        candidates = [h for h in hosts if h.name != leader_host]
        if candidates:
            target = min(candidates, key=lambda h: load[h.name])
        else:
            target = hosts[0]
            warnings.append(
                f"standby for {leader} shares host {target.name} with its leader — "
                f"only one host is available, so losing that host loses both. "
                f"This is a test topology, not an HA one."
            )
        name = standby_name(leader)
        assignment[name] = target.name
        load[target.name] += 1
        standby_entries.append((name, leader))

    # --- allocate ports per host --------------------------------------
    # Ports are assigned in the order nodes were placed, so a given inventory
    # and node count always produce the same layout.
    ordered = names + [name for name, _ in standby_entries]
    per_host_index = {host.name: 0 for host in hosts}
    ports = {}
    for name in ordered:
        host_name = assignment[name]
        offset = per_host_index[host_name]
        ports[name] = (base_pg_port + offset, base_restapi_port + offset)
        per_host_index[host_name] = offset + 1

    # --- build nodes --------------------------------------------------
    host_by_name = {host.name: host for host in hosts}
    nodes = []

    for name in names:
        host = host_by_name[assignment[name]]
        pg_port, restapi_port = ports[name]
        nodes.append(
            Node(
                name=name,
                role=ROLE_SPOCK,
                host=host.name,
                address=host.address,
                pg_port=pg_port,
                restapi_port=restapi_port,
                data_dir=f"{data_root}/{name}",
                # Every Spock node is its own Patroni scope: it may only fail
                # over into its own physical standby, never into a peer.
                scope=f"{cluster_name}-{name}",
                config_file=f"/etc/patroni/{name}.yml",
                pgpass_file=f"{data_root}/pgpass_{name}",
                standbys=[],
            )
        )

    for name, leader in standby_entries:
        host = host_by_name[assignment[name]]
        pg_port, restapi_port = ports[name]
        leader_node = next(n for n in nodes if n.name == leader)
        leader_node.standbys.append(name)
        nodes.append(
            Node(
                name=name,
                role=ROLE_STANDBY,
                host=host.name,
                address=host.address,
                pg_port=pg_port,
                restapi_port=restapi_port,
                data_dir=f"{data_root}/{name}",
                scope=leader_node.scope,
                config_file=f"/etc/patroni/{name}.yml",
                pgpass_file=f"{data_root}/pgpass_{name}",
                leader=leader,
            )
        )

    # --- etcd ---------------------------------------------------------
    etcd_members, etcd_note = etcd_management.select_members(hosts)
    member_names = {host.name for host in etcd_members}
    for host in hosts:
        host.is_etcd_member = host.name in member_names
    if len(etcd_members) == 1:
        warnings.append(etcd_note)

    plan = ClusterPlan(
        cluster_name=cluster_name,
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
        pg_major=str(pg_major),
        pg_version=pg_version,
        spock_major=str(spock_major),
        repo_channel=repo_channel,
        deploy_mode=deploy_mode,
        node_count=node_count,
        standby_of=standby_of,
        base_pg_port=base_pg_port,
        base_restapi_port=base_restapi_port,
        data_root=data_root,
        zodan_sql=zodan_sql,
        etcd_endpoints=etcd_management.endpoints(etcd_members),
        extra_hba_cidrs=list(extra_hba_cidrs or []),
        hosts=list(hosts),
        nodes=nodes,
        source_build=source_build or {},
        run_id=run_id,
    )

    _check_port_collisions(plan)
    return plan, warnings


def _check_port_collisions(plan):
    """Fail early if two nodes on one host would claim the same port."""
    seen = {}
    for node in plan.nodes:
        for label, port in (("postgres", node.pg_port),
                            ("patroni REST", node.restapi_port)):
            key = (node.host, label, port)
            if key in seen:
                raise TopologyError(
                    f"port collision on {node.host}: {node.name} and {seen[key]} "
                    f"both want {label} port {port}"
                )
            seen[key] = node.name

    # etcd's ports are fixed, so a node landing on one is a configuration error.
    reserved = {etcd_management.CLIENT_PORT, etcd_management.PEER_PORT}
    for node in plan.nodes:
        for port in (node.pg_port, node.restapi_port):
            if port in reserved and plan.host(node.host).is_etcd_member:
                raise TopologyError(
                    f"{node.name} would use port {port} on {node.host}, which "
                    f"etcd reserves. Choose a different --base-port."
                )


def apply_platform(plan, host, platform_info):
    """Record a probed host's platform on the plan and its nodes."""
    host.platform = platform_info
    host.family = platform_info["family"]

    if plan.deploy_mode == "source":
        from aspects import source_build
        host.bin_dir = source_build.bin_dir(plan.pg_major)
    else:
        host.bin_dir = platform_detect.pg_bin_dir(host.family, plan.pg_major)

    for node in plan.nodes_on(host.name):
        node.family = host.family
        node.bin_dir = host.bin_dir
    return host.bin_dir


def describe_standby_choice(plan):
    """Explain each standby's placement, for the report and the summary."""
    lines = []
    for node in plan.standby_nodes:
        leader = plan.node(node.leader)
        same_host = node.host == leader.host
        lines.append(
            f"{node.name} follows {leader.name}: "
            + (
                f"same host ({node.host}) — no host-failure protection"
                if same_host
                else f"{node.host} vs leader on {leader.host} — survives host loss"
            )
        )
    return lines
