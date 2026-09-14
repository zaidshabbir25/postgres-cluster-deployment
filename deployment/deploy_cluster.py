#!/usr/bin/env python3
"""End-to-end cluster deployment.

The order below is not arbitrary; each step depends on the one before it:

  prerequisites  -> a usable package manager and no PGDG shadowing pgEdge
  packages       -> spock.so must exist before Patroni bootstraps, because the
                    bootstrap sets shared_preload_libraries = spock
  auth           -> .pgpass must exist before anything runs psql
  etcd           -> Patroni cannot elect a leader without a DCS
  patroni leaders-> a standby clones from a running leader, so leaders first
  patroni standbys
  spock crosswire-> needs every Spock node up and reachable from its peers
  verification   -> the deployment is not done until replication is proven

Every step is recorded in the run's step timeline whether it passes or fails, so
a partial deployment still produces a report that says how far it got.
"""

import time
import traceback

from aspects import (
    auth_setup,
    configure_repository,
    etcd_management,
    health,
    inventory,
    package_management,
    patroni_management,
    pg_server_management,
    platform_detect,
    prereq_setup,
    source_build,
    spock_management,
    state,
)
from aspects.logging_setup import RunLogger, new_run_id
from aspects.ssh_executor import build_executor
from deployment import topology


class DeploymentFailed(RuntimeError):
    """Raised when a step that the rest of the deployment depends on fails."""


class ClusterDeployer:
    def __init__(self, plan, run_logger=None, clean=False, skip_verify=False):
        self.plan = plan
        self.log = run_logger or RunLogger(new_run_id(plan.cluster_name))
        self.plan.run_id = self.log.run_id
        self.clean = clean
        self.skip_verify = skip_verify

        self.executors = {}
        self.results = {}
        self.warnings = []

    # ------------------------------------------------------------------
    # Executor plumbing
    # ------------------------------------------------------------------

    def executor_for_host(self, host_name):
        if host_name not in self.executors:
            host = self.plan.host(host_name)
            executor = build_executor(
                {
                    "name": host.name,
                    "host": host.address,
                    "username": host.username,
                    "key_file": host.key_file,
                    "port": host.port,
                    "local": host.local,
                },
                run_logger=self.log,
            )
            executor.connect()
            self.executors[host_name] = executor
        return self.executors[host_name]

    def executor_for_node(self, node):
        return self.executor_for_host(node.host)

    def close(self):
        for executor in self.executors.values():
            try:
                executor.close()
            except Exception:
                pass
        self.executors.clear()

    # ------------------------------------------------------------------
    # Step wrapper
    # ------------------------------------------------------------------

    def _step(self, name, detail, function, required=True):
        """Run one step, recording its outcome.

        A failed required step aborts the deployment; a failed optional step is
        recorded as a warning and the run continues.
        """
        self.log.step_start(name, detail)
        try:
            message = function()
            self.log.step_end("passed", message or "")
            return True
        except Exception as exc:
            summary = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
            self.log.step_end("failed", summary)
            self.log.debug(traceback.format_exc())
            if required:
                raise DeploymentFailed(f"{name}: {summary}") from exc
            self.warnings.append(f"{name}: {summary}")
            return False

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def step_connect(self):
        """Reach every host and record what it is running."""
        detected = []
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)
            info = platform_detect.detect(executor)
            bin_dir = topology.apply_platform(self.plan, host, info)
            detected.append(f"{host.name}={info['family']} ({info['pretty']})")
            self.log.info(f"    {host.name}: {info['pretty']} {info['arch']} "
                          f"-> binaries in {bin_dir}")

        families = {host.family for host in self.plan.hosts}
        if len(families) > 1:
            self.warnings.append(
                f"mixed platform families in one cluster ({', '.join(sorted(families))}) "
                f"— supported, but package versions may differ between nodes"
            )
        return "; ".join(detected)

    def step_clean(self):
        """Remove the previous deployment's state so this run starts fresh."""
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)

            for node in self.plan.nodes_on(host.name):
                patroni_management.stop(executor, node.name)
            if host.is_etcd_member:
                etcd_management.purge(executor, node=host.name)

            for node in self.plan.nodes_on(host.name):
                executor.try_run(f"rm -rf {node.data_dir}", node=node.name)
                executor.try_run(f"rm -f {node.config_file}", node=node.name)
        return "previous Patroni instances stopped, data directories and etcd state removed"

    def step_prerequisites(self):
        messages = []
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)
            info, message = prereq_setup.install_prerequisites(executor, node=host.name)
            host.platform = info
            messages.append(f"{host.name}: {message}")
            self.log.info(f"    {host.name}: {message}")
        return "; ".join(messages)

    def step_repository(self):
        messages = []
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)
            message = configure_repository.configure(
                executor, host.family, self.plan.repo_channel, node=host.name
            )
            messages.append(f"{host.name}: {message}")
            self.log.info(f"    {host.name}: {message}")
        return f"channel={self.plan.repo_channel} on {len(self.plan.hosts)} host(s)"

    def step_install_server(self):
        """Install PostgreSQL + Spock (+ contrib for dblink)."""
        installed = {}
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)
            packages = platform_detect.server_packages(
                host.family, self.plan.pg_major, self.plan.spock_major
            )
            self.log.info(f"    {host.name}: installing {', '.join(packages)}")
            names, _ = package_management.install(
                executor, host.family, packages, node=host.name
            )
            installed[host.name] = names

            version = pg_server_management.server_version(
                executor, host.bin_dir, node=host.name
            )
            if not version:
                raise RuntimeError(
                    f"{host.name}: no PostgreSQL server found at {host.bin_dir} "
                    f"after installing {', '.join(packages)}"
                )
            if not self.plan.pg_version:
                self.plan.pg_version = version
            for node in self.plan.nodes_on(host.name):
                node.pg_version = version
            self.log.info(f"    {host.name}: PostgreSQL {version}")

        self._record_package_inventory()
        return f"PostgreSQL {self.plan.pg_version} + spock{self.plan.spock_major}"

    def step_install_patroni(self):
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)
            packages = list(platform_detect.patroni_packages(host.family))
            # etcd is only needed where a member actually runs, but etcdctl is
            # how every host queries DCS health, so install the package widely.
            self.log.info(f"    {host.name}: installing {', '.join(packages)}")
            package_management.install(executor, host.family, packages, node=host.name)

        self._record_package_inventory()
        return f"Patroni and etcd installed on {len(self.plan.hosts)} host(s)"

    def step_source_build(self):
        """Build PostgreSQL and Spock from source on every host."""
        details = {}
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)
            self.log.info(f"    {host.name}: starting source build (this takes a while)")
            details[host.name] = source_build.build_host(
                executor, host, self.plan, run_logger=self.log
            )

            version = pg_server_management.server_version(
                executor, host.bin_dir, node=host.name
            )
            if not version:
                raise RuntimeError(
                    f"{host.name}: built PostgreSQL is not runnable at {host.bin_dir}"
                )
            if not self.plan.pg_version:
                self.plan.pg_version = version
            for node in self.plan.nodes_on(host.name):
                node.pg_version = version

        self.results["source_build"] = details
        return f"built PostgreSQL {self.plan.pg_version} and Spock from source"

    def step_auth(self):
        """Passwordless psql everywhere: .pgpass, pg_service.conf, PATH."""
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)
            message = auth_setup.configure_host(
                executor, host.family, self.plan.nodes,
                self.plan.db_user, self.plan.db_password, self.plan.db_name,
                host.bin_dir, node=host.name,
            )
            self.log.info(f"    {host.name}: {message}")
        return (
            f"psql runs without a password on all hosts; nodes also reachable as "
            f"`psql service=<node>`"
        )

    def step_etcd(self):
        members = self.plan.etcd_hosts
        _, note = etcd_management.select_members(self.plan.hosts)
        self.log.info(f"    {note}")

        # A source build installs its own etcd unit, which reads YAML on both
        # families; package mode must match each family's packaged unit instead.
        config_format = "yaml" if self.plan.deploy_mode == "source" else None

        for host in members:
            executor = self.executor_for_host(host.name)
            path = etcd_management.configure(
                executor, host, members, self.plan.cluster_name, node=host.name,
                config_format=config_format,
            )
            self.log.info(f"    {host.name}: etcd config written to {path}")

        # Members must all be launched before any of them can form a quorum, so
        # start them all and only then wait for health.
        for host in members:
            executor = self.executor_for_host(host.name)
            etcd_management.start(executor, host, members, node=host.name)
            self.log.info(f"    {host.name}: etcd running")

        first = self.executor_for_host(members[0].name)
        status = etcd_management.cluster_status(first, members, node=members[0].name)
        if not status["ok"]:
            raise RuntimeError(
                f"etcd has no quorum: {status['healthy_count']}/{status['member_count']} "
                f"members healthy\n{status['health']}"
            )
        self.results["etcd"] = status
        return (
            f"{status['healthy_count']}/{status['member_count']} members healthy at "
            f"{', '.join(self.plan.etcd_endpoints)}"
        )

    def step_patroni_config(self):
        for node in self.plan.nodes:
            executor = self.executor_for_node(node)
            patroni_management.write_config(executor, self.plan, node,
                                            run_logger=self.log)
            unit = patroni_management.write_service_unit(executor, self.plan, node)
            self.log.info(f"    {node.name}: {node.config_file} and {unit}")
            problem = patroni_management.validate_config(executor, self.plan, node)
            if problem:
                self.log.warn(f"{node.name}: patroni --validate-config says:\n{problem}")
        return f"configuration written for {len(self.plan.nodes)} node(s)"

    def step_start_leaders(self):
        """Bootstrap each Spock node as the leader of its own scope."""
        leaders = self.plan.spock_nodes

        for node in leaders:
            executor = self.executor_for_node(node)
            if self.clean:
                # A reused scope name inherits the old cluster's leader key and
                # system identifier, and every new member then refuses to start.
                patroni_management.remove_scope(executor, node, node_name=node.name)
            conflict = patroni_management.port_conflict(executor, node)
            if conflict:
                self.log.warn(f"{node.name}: {conflict}")
            message = patroni_management.start(executor, self.plan, node,
                                               run_logger=self.log)
            self.log.info(f"    {node.name}: {message}")

        failures = []
        for node in leaders:
            executor = self.executor_for_node(node)
            ok, role, detail = patroni_management.wait_for_role(
                executor, node, ["Leader", "Master", "Primary"],
                timeout=420, run_logger=self.log,
            )
            if ok:
                self.log.info(f"    {node.name}: {role} of scope {node.scope}")
            else:
                logs = patroni_management.logs(executor, node.name)
                failures.append(f"{node.name} never became leader ({detail})\n{logs}")

        if failures:
            raise RuntimeError("\n\n".join(failures))
        return f"{len(leaders)} Spock node(s) bootstrapped as scope leaders"

    def step_start_standbys(self):
        standbys = self.plan.standby_nodes
        if not standbys:
            return "no standbys requested"

        for node in standbys:
            executor = self.executor_for_node(node)
            conflict = patroni_management.port_conflict(executor, node)
            if conflict:
                self.log.warn(f"{node.name}: {conflict}")
            message = patroni_management.start(executor, self.plan, node,
                                               run_logger=self.log)
            self.log.info(
                f"    {node.name}: {message} — cloning from {node.leader} "
                f"via pg_basebackup"
            )

        failures = []
        for node in standbys:
            executor = self.executor_for_node(node)
            # A clone copies the whole leader data directory, so this is the
            # slowest wait in the deployment.
            ok, role, detail = patroni_management.wait_for_role(
                executor, node, ["Replica", "Sync Standby"],
                timeout=900, run_logger=self.log,
            )
            if ok:
                self.log.info(
                    f"    {node.name}: {role} in scope {node.scope} "
                    f"(following {node.leader})"
                )
            else:
                logs = patroni_management.logs(executor, node.name)
                failures.append(f"{node.name} never joined as a replica ({detail})\n{logs}")

        if failures:
            raise RuntimeError("\n\n".join(failures))
        return f"{len(standbys)} standby node(s) streaming from their leaders"

    def step_validate_patroni(self):
        problems, scopes = [], {}
        for scope, members in self.plan.scopes().items():
            leader = next(m for m in members if m.is_spock)
            executor = self.executor_for_node(leader)
            status = patroni_management.cluster_status(executor, leader,
                                                       node_name=leader.name)
            scopes[scope] = status

            expected = {m.name for m in members}
            seen = {m["name"] for m in status["members"]}
            if expected - seen:
                problems.append(
                    f"{scope}: members missing from the DCS: "
                    f"{', '.join(sorted(expected - seen))}"
                )
            if not status["leader"]:
                problems.append(f"{scope}: no leader")

            roles = ", ".join(
                f"{m['name']}={m['role']}/{m['state']}" for m in status["members"]
            )
            self.log.info(f"    {scope}: {roles}")

        self.results["scopes"] = scopes
        if problems:
            raise RuntimeError("; ".join(problems))
        return f"{len(scopes)} Patroni scope(s) healthy"

    def step_spock_extensions(self):
        for node in self.plan.spock_nodes:
            executor = self.executor_for_node(node)
            pg_server_management.wait_for_ready(
                executor, node.bin_dir, node.pg_port, self.plan.db_user,
                timeout=120, node=node.name,
            )
            extensions = spock_management.create_extensions(
                executor, self.plan, node, run_logger=self.log
            )
            version = spock_management.spock_version(executor, self.plan, node)
            self.log.info(
                f"    {node.name}: spock {version or 'unknown'} "
                f"({', '.join(extensions)})"
            )
        return f"spock and dblink created on {len(self.plan.spock_nodes)} node(s)"

    def step_load_zodan(self):
        """Install zodan's procedures on every Spock node.

        Loaded everywhere, not just where add_node runs, so any node can later
        run spock.health_check or add a further node without a reload.
        """
        script = None
        for node in self.plan.spock_nodes:
            executor = self.executor_for_node(node)
            script = spock_management.load_zodan(
                executor, self.plan, node, run_logger=self.log
            )
            self.log.info(f"    {node.name}: zodan procedures loaded")
        self.plan.zodan_sql = script or self.plan.zodan_sql
        return f"{script} loaded on {len(self.plan.spock_nodes)} node(s)"

    def step_crosswire(self):
        """Cross-wire every Spock node into the cluster, one join at a time."""
        spock_nodes = self.plan.spock_nodes
        if len(spock_nodes) < 2:
            return "single-node cluster — nothing to cross-wire"

        source = spock_nodes[0]
        joined = [source.name]

        for node in spock_nodes[1:]:
            executor = self.executor_for_node(node)
            ok, output = spock_management.add_node(
                executor, self.plan, source, node, run_logger=self.log
            )
            self.log.node(node.name, output)
            if not ok:
                rate = spock_management.parse_success_rate(output)
                raise RuntimeError(
                    f"cross-wiring {node.name} via {source.name} failed"
                    + (f" (zodan success rate {rate}%)" if rate is not None else "")
                    + f". Full zodan output is in "
                      f"{self.log.node_log_path(node.name)}"
                )
            joined.append(node.name)
            self.log.info(
                f"    {node.name} joined — cluster is now "
                f"{' <-> '.join(joined)}"
            )
            # Spock's apply workers need a moment to settle before the next join
            # inspects the cluster.
            time.sleep(5)

        return f"{len(joined)} nodes cross-wired in a full mesh"

    def step_ddl_replication(self):
        for node in self.plan.spock_nodes:
            executor = self.executor_for_node(node)
            spock_management.enable_ddl_replication(
                executor, self.plan, node, run_logger=self.log
            )
        return "automatic DDL replication enabled on all Spock nodes"

    def step_verify(self):
        """Prove replication actually works before calling the deployment done."""
        ok, findings = spock_management.verify_replication(
            self.executor_for_node, self.plan, run_logger=self.log
        )
        self.results["replication"] = findings
        if not ok:
            raise RuntimeError("; ".join(findings["problems"]))
        return (
            f"all {len(self.plan.spock_nodes)} Spock nodes see each other and "
            f"every subscription is replicating"
        )

    def step_snapshot(self):
        """Take the first health snapshot, which seeds the dashboard."""
        snapshot = health.snapshot(self.plan, run_logger=self.log)
        self.results["health"] = snapshot
        return (
            f"cluster status {snapshot['status']} — "
            f"{snapshot['summary']['nodes_ok']}/{snapshot['summary']['nodes']} "
            f"nodes ok"
        )

    def _record_package_inventory(self):
        packages = self.results.setdefault("packages", {})
        for host in self.plan.hosts:
            executor = self.executor_for_host(host.name)
            packages[host.name] = package_management.list_pgedge_packages(
                executor, host.family, node=host.name
            )

    # ------------------------------------------------------------------
    # Driver
    # ------------------------------------------------------------------

    def run(self):
        """Execute the deployment. Returns a result dict; never raises."""
        started = time.time()
        outcome = "succeeded"
        failure = None

        self.log.banner(f"Deploying cluster '{self.plan.cluster_name}'")
        for line in self.plan.summary_lines():
            self.log.info(line)

        try:
            self._step("Connect to hosts",
                       f"{len(self.plan.hosts)} host(s) over SSH",
                       self.step_connect)

            if self.clean:
                self._step("Clean previous deployment",
                           "stop Patroni, wipe data directories and etcd state",
                           self.step_clean, required=False)

            self._step("Install prerequisites",
                       "base tools, package manager preparation, PGDG muted",
                       self.step_prerequisites)

            if self.plan.deploy_mode == "source":
                self._step("Build PostgreSQL and Spock from source",
                           "patch, configure, make, install; Patroni via pip, "
                           "etcd from official release",
                           self.step_source_build)
            else:
                self._step("Configure pgEdge repository",
                           f"channel {self.plan.repo_channel}",
                           self.step_repository)
                self._step("Install PostgreSQL and Spock packages",
                           f"pg{self.plan.pg_major} + spock{self.plan.spock_major} "
                           f"+ contrib (dblink)",
                           self.step_install_server)
                self._step("Install Patroni and etcd packages",
                           "high availability and the DCS behind it",
                           self.step_install_patroni)

            self._step("Set up passwordless psql",
                       ".pgpass, pg_service.conf and pg_hba for every node",
                       self.step_auth)

            self._step("Configure and start etcd",
                       f"{len(self.plan.etcd_hosts)} member(s)",
                       self.step_etcd)

            self._step("Write Patroni configuration",
                       f"{len(self.plan.nodes)} node(s), "
                       f"{len(self.plan.scopes())} scope(s)",
                       self.step_patroni_config)

            self._step("Bootstrap Spock nodes with Patroni",
                       f"{len(self.plan.spock_nodes)} scope leader(s)",
                       self.step_start_leaders)

            if self.plan.standby_nodes:
                self._step("Add standby nodes",
                           ", ".join(
                               f"{n.name} follows {n.leader}"
                               for n in self.plan.standby_nodes
                           ),
                           self.step_start_standbys)

            self._step("Validate Patroni cluster",
                       "every scope has a leader and all members are streaming",
                       self.step_validate_patroni)

            self._step("Create Spock extensions",
                       "spock and dblink on every Spock node",
                       self.step_spock_extensions)

            self._step("Load zodan procedures",
                       "the cross-wiring library used by spock.add_node",
                       self.step_load_zodan)

            self._step("Cross-wire Spock nodes",
                       "spock.add_node joins each node to every peer, both ways",
                       self.step_crosswire)

            self._step("Enable DDL replication",
                       "schema changes propagate automatically",
                       self.step_ddl_replication,
                       required=False)

            if not self.skip_verify:
                self._step("Verify replication",
                           "node visibility and subscription state on every node",
                           self.step_verify)

            self._step("Collect health snapshot",
                       "seeds the dashboard and the report",
                       self.step_snapshot, required=False)

        except DeploymentFailed as exc:
            outcome = "failed"
            failure = str(exc)
            self.log.info("")
            self.log.error(f"Deployment failed — {failure}")
        except Exception as exc:  # pragma: no cover - unexpected failure path
            outcome = "failed"
            failure = f"unexpected error: {exc}"
            self.log.error(failure)
            self.log.debug(traceback.format_exc())

        duration = round(time.time() - started, 1)
        counts = self.log.counts()

        # Save state even on failure: a half-built cluster still needs to be
        # inspectable and cleanable.
        try:
            state_file = state.save(
                self.plan,
                extra={
                    "run_id": self.log.run_id,
                    "outcome": outcome,
                    "duration": duration,
                    "log_dir": str(self.log.root),
                    "warnings": self.warnings,
                },
            )
        except Exception as exc:
            state_file = None
            self.warnings.append(f"could not save cluster state: {exc}")

        result = {
            "run_id": self.log.run_id,
            "cluster": self.plan.cluster_name,
            "outcome": outcome,
            "failure": failure,
            "duration": duration,
            "steps": self.log.steps,
            "counts": counts,
            "warnings": self.warnings,
            "results": self.results,
            "plan": self.plan,
            "log_dir": str(self.log.root),
            "state_file": str(state_file) if state_file else None,
        }

        self._print_summary(result)
        return result

    def _print_summary(self, result):
        self.log.banner(f"Deployment {result['outcome']} in {result['duration']}s")
        counts = result["counts"]
        self.log.info(
            f"Steps: {counts.get('passed', 0)} passed, "
            f"{counts.get('failed', 0)} failed, "
            f"{counts.get('skipped', 0)} skipped"
        )

        if result["outcome"] == "succeeded":
            self.log.info("")
            self.log.info("Connect to any node without a password:")
            for node in self.plan.spock_nodes:
                self.log.info(
                    f"  psql -h {node.address} -p {node.pg_port} "
                    f"-U {self.plan.db_user} -d {self.plan.db_name}"
                    f"    # or: psql service={node.name}"
                )

        if result["warnings"]:
            self.log.info("")
            self.log.info(f"Warnings ({len(result['warnings'])}):")
            for warning in result["warnings"]:
                self.log.info(f"  - {warning}")

        self.log.info("")
        self.log.info(f"Logs: {result['log_dir']}")


# ---------------------------------------------------------------------------
# Entry point used by the shell wrapper
# ---------------------------------------------------------------------------


def deploy(options):
    """Build a plan from resolved options and deploy it.

    `options` is the dict pg_deploy_cluster.sh's Python side assembles from
    flags and interactive answers.
    """
    hosts, defaults = inventory.load(options.get("inventory"))

    for warning in inventory.check_key_permissions(hosts):
        print(f"WARN  {warning}")

    run_id = new_run_id(options.get("cluster_name") or "cluster")
    run_logger = RunLogger(run_id)

    pins = inventory.expected_versions(
        options.get("pg_major") or defaults.get("pg_major", "17"),
        options.get("spock_major") or defaults.get("spock_major", "50"),
    )

    plan, warnings = topology.plan_cluster(
        hosts=hosts,
        cluster_name=options.get("cluster_name") or defaults.get("cluster_name", "pgedge"),
        node_count=int(options.get("node_count") or defaults.get("node_count", 2)),
        standby_of=options.get("standby_of") or defaults.get("standby_of", []),
        db_name=options.get("db_name") or defaults.get("db_name", "postgres"),
        db_user=options.get("db_user") or defaults.get("db_user", "postgres"),
        db_password=options.get("db_password") or defaults.get("db_password", "postgres"),
        pg_major=options.get("pg_major") or defaults.get("pg_major", "17"),
        pg_version=options.get("pg_version") or pins.get("pg_version", ""),
        spock_major=options.get("spock_major") or defaults.get("spock_major", "50"),
        repo_channel=options.get("repo_channel") or defaults.get("repo_channel", "release"),
        deploy_mode=options.get("deploy_mode") or "packages",
        base_pg_port=int(options.get("base_port") or defaults.get("base_port", 5432)),
        base_restapi_port=int(
            options.get("base_restapi_port") or defaults.get("base_restapi_port", 8008)
        ),
        data_root=options.get("data_root") or defaults.get("data_root", topology.DEFAULT_DATA_ROOT),
        extra_hba_cidrs=options.get("hba_cidrs") or defaults.get("hba_cidrs", []),
        source_build=options.get("source_build") or defaults.get("source_build", {}),
        zodan_sql=options.get("zodan_sql") or pins.get("zodan_sql", ""),
        run_id=run_id,
    )

    for warning in warnings:
        run_logger.warn(warning)

    deployer = ClusterDeployer(
        plan,
        run_logger=run_logger,
        clean=bool(options.get("clean")),
        skip_verify=bool(options.get("skip_verify")),
    )
    deployer.warnings.extend(warnings)

    try:
        return deployer.run()
    finally:
        deployer.close()
