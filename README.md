# postgres-cluster-deployment

Deploy an **n-node PostgreSQL cluster** with [Spock](https://github.com/pgEdge/spock)
multi-master replication and [Patroni](https://patroni.readthedocs.io/) high
availability, onto machines you already have, over SSH.

Answer four questions and you get a cross-wired multi-master cluster with
per-node failover, passwordless `psql` access everywhere, an HTML report of
exactly what happened, and a live health dashboard.

```console
$ ./pg_deploy_cluster.sh
1) How should PostgreSQL and Spock be installed?
2) How many Spock nodes should the cluster have?
3) Which PostgreSQL version?
4) Which nodes should get a Patroni standby?
```

---

## What it builds

Two different replication layers do two different jobs, and keeping them
straight is the key to understanding this tool.

**Spock** gives you *multi-master*: every Spock node accepts writes and
replicates logically to every other Spock node, in both directions. Nodes are
cross-wired into a full mesh.

**Patroni** gives you *high availability within one node*: each Spock node is
the leader of its own Patroni scope, and its standbys are physical replicas of
that node alone. If `n1` dies, Patroni promotes `n1s1` — it never promotes `n2`,
because `n2` is a peer holding different data, not a copy.

```
                Spock multi-master mesh — logical, bidirectional
        ┌───────────────────────────────────────────────────────────┐
        │                                                           │
   ┌────▼───┐                  ┌────────┐                   ┌───────▼┐
   │   n1   │◀────────────────▶│   n2   │◀─────────────────▶│   n3   │
   └────┬───┘                  └────┬───┘                   └────────┘
        │ streaming replication     │ streaming replication
   ┌────▼───┐                  ┌────▼───┐
   │  n1s1  │                  │  n2s1  │
   └────────┘                  └────────┘

   scope demo-n1               scope demo-n2               scope demo-n3
   (leader + standby)          (leader + standby)          (leader only)
```

Every Spock node is its own scope, so a failover is always contained: it changes
which machine serves `n1`, never which data `n1` holds.

### Placement

One Spock node per host. If you ask for more nodes than you have hosts, the
surplus is packed onto the least-loaded hosts on separate ports (5432, 5433, …)
— useful for a single-VM test cluster, and reported as a shared failure domain
so nobody mistakes it for HA. A standby is always placed on a *different* host
from its leader when one is free; when it cannot be, the plan says so.

`etcd` (the store Patroni elects leaders through) is sized from the host count:
three or more hosts get a 3-member cluster that survives losing a machine, fewer
get a single member that is flagged as a single point of failure.

---

## Requirements

**Control machine** (where you run these scripts) — Python 3.9+ and network
reach to your hosts. `./pg_deploy_cluster.sh` creates a virtualenv and installs
`paramiko`, `PyYAML` and `Flask` on first run.

**Target hosts** — a RHEL-family (RHEL, Rocky, AlmaLinux, Oracle Linux) or
Debian-family (Debian, Ubuntu) machine, reachable over SSH, with a login user
that has **passwordless sudo**. Every step runs through `sudo -n`.

Hosts must reach each other on the PostgreSQL ports (5432+), the Patroni REST
ports (8008+) and etcd's ports (2379/2380). On AWS that means a security group
allowing those ports between the instances.

---

## Quick start

```bash
git clone <this repo> && cd postgres-cluster-deployment

# 1. Describe your machines.
cp configuration/inventory.example.json configuration/inventory.json
$EDITOR configuration/inventory.json

# 2. Deploy. Answers the four questions, shows the plan, waits for confirmation.
./pg_deploy_cluster.sh

# 3. Watch it.
./pg_cluster_status.sh
./pg_dashboard.sh          # http://127.0.0.1:8080
```

### The inventory

The inventory lists **hosts only** — how many nodes land on them is decided per
deployment. That separation is what lets one inventory serve a 2-node and a
6-node cluster.

```json
{
  "defaults": { "cluster_name": "pgedge", "pg_major": "17", "spock_major": "50" },
  "hosts": [
    { "name": "node-a", "host": "10.0.1.11", "username": "rocky",  "enabled": true },
    { "name": "node-b", "host": "10.0.1.12", "username": "rocky",  "enabled": true },
    { "name": "node-c", "host": "10.0.1.13", "username": "ubuntu", "enabled": false }
  ]
}
```

`defaults` supplies fallbacks for anything you do not pass on the command line.
Set `"enabled": false` to park a host without deleting it.

**SSH keys.** Leave `key_file` empty and `ssh-add` your key — that is the
default and nothing sensitive touches the repo. Otherwise set
`PG_CLUSTER_SSH_KEY`, or put a key in `keys/` (gitignored) and `chmod 600` it.
`configuration/inventory.json` is gitignored too, because it names your
machines.

### Rehearse without touching anything

```bash
./pg_deploy_cluster.sh --dry-run --nodes 3 --standby n1,n2
```

```
NODE       ROLE      HOST             ADDRESS          PG      API    SCOPE       FOLLOWS
n1         spock     node-a           10.0.1.11        5432    8008   demo-n1     -
n2         spock     node-b           10.0.1.12        5432    8008   demo-n2     -
n3         spock     node-c           10.0.1.13        5432    8008   demo-n3     -
n1s1       standby   node-b           10.0.1.12        5433    8009   demo-n1     n1
n2s1       standby   node-a           10.0.1.11        5433    8009   demo-n2     n2

Standby placement:
  n1s1 follows n1: node-b vs leader on node-a — survives host loss
  n2s1 follows n2: node-a vs leader on node-b — survives host loss
```

---

## Project layout

```
pg_deploy_cluster.sh      Interactive entry point — asks the four questions
pg_cluster_status.sh      Cluster health in the terminal; exits non-zero if unhealthy
pg_dashboard.sh           Starts the live web dashboard

aspects/                  Reusable building blocks. Each module owns one concern
                          and takes an executor as its first argument, so the same
                          code drives one VM or twenty.
  ssh_executor.py           Every remote command goes through here
  platform_detect.py        RHEL vs Debian: package names, paths, toolchain
  prereq_setup.py           Base tools, apt-lock handling, PGDG muted
  configure_repository.py   pgEdge repo: release / staging / daily
  package_management.py     Install, query, version-normalise, remove
  source_build.py           Build PostgreSQL + Spock from source
  pg_server_management.py   Cluster lifecycle, psql, GUC version gating
  auth_setup.py             .pgpass, pg_service.conf, pg_hba — passwordless psql
  etcd_management.py        The DCS Patroni elects through
  patroni_management.py     Config rendering, startup, scope inspection
  spock_management.py       Extensions, zodan, cross-wiring, replication checks
  service_management.py     systemd, degrading gracefully where it is absent
  cluster_model.py          Host / Node / ClusterPlan — the shared data model
  inventory.py              Loads and validates the inventory and version pins
  state.py                  Persisted record of what has been deployed
  health.py                 Collects one full cluster health snapshot
  logging_setup.py          Run-scoped logs and the step timeline

deployment/               Orchestration: what to do, in what order
  topology.py               Inventory + node count -> concrete layout
  deploy_cluster.py         The end-to-end deployment
  add_standby.py            Add a standby to a live cluster
  cleanup.py                Tear a cluster down
  cli.py                    Every action, as a flag, for CI

configuration/            Everything you configure
  inventory.example.json    Copy to inventory.json
  config16..19.env          Version pins per PostgreSQL major
  spock/                    zodan cross-wiring SQL (see its README)
  clusters/                 Saved state per deployed cluster (generated)

reports/                  Per-run HTML + JSON + text reports, and an index
dashboard/                Flask app, template, CSS and JS for the live view
logs/                     Per-run transcripts: deploy.log, <node>.log, steps.json
```

The `aspects/` and `deployment/` split is the point of the design: `aspects`
knows *how* to do things to a host, `deployment` decides *what* to do. Nothing
in `aspects` imports from `deployment`.

---

## Deployment modes

### Native packages (default)

Installs from the pgEdge repository. Spock is installed **first** on purpose: it
pulls in the matching pgEdge PostgreSQL server as a dependency, which guarantees
the two were built against the same tree. `contrib` is installed separately
because zodan's cross-wiring calls `dblink`.

Community PGDG repositories are disabled during prerequisites. Both PGDG and
pgEdge ship `postgresql<major>-*` packages, and if PGDG is enabled the resolver
can satisfy Spock's dependency from PGDG — which installs cleanly and then fails
to load `spock.so`.

```bash
./pg_deploy_cluster.sh --nodes 3 --pg-major 17 --spock-major 50 --channel release
```

### Manual build from source

Builds PostgreSQL and Spock on every host. Spock needs core patches, so the
order is fixed: the PostgreSQL tree is patched from `spock/patches/<major>/`
**before** it is configured, then Spock is built against the result with
`USE_PGXS=1`. A Spock built against an unpatched server compiles and then fails
at load time.

Needs an exact version, because it downloads a release tarball. Expect 20–40
minutes per host.

```bash
./pg_deploy_cluster.sh --mode source --pg-version 17.11 --spock-branch main
```

Patroni and etcd are *not* built from source: Patroni goes into its own
virtualenv (so it cannot break the system interpreter) and etcd is installed
from its official static release. Neither benefits from a source build, and
pulling in a Go toolchain for etcd would double the prerequisites.

---

## What a deployment does

```
 1  Connect to hosts               SSH, and probe each platform
 2  Install prerequisites          base tools, apt lock released, PGDG muted
 3  Configure pgEdge repository    (packages mode)
 4  Install PostgreSQL and Spock   spock first, then contrib for dblink
 5  Install Patroni and etcd
 6  Set up passwordless psql       .pgpass, pg_service.conf, pg_hba
 7  Configure and start etcd       1 or 3 members, then wait for quorum
 8  Write Patroni configuration    one config and one systemd unit per node
 9  Bootstrap Spock nodes          each becomes leader of its own scope
10  Add standby nodes              clone from their leader via pg_basebackup
11  Validate Patroni cluster       every scope has a leader, all members stream
12  Create Spock extensions        spock and dblink
13  Load zodan procedures          the cross-wiring library
14  Cross-wire Spock nodes         spock.add_node joins each node to every peer
15  Enable DDL replication         schema changes propagate automatically
16  Verify replication             node visibility and subscription state
17  Collect health snapshot        seeds the dashboard and the report
```

Steps 3–5 are replaced by a single source-build step in `--mode source`.

Every step is recorded whether it passes or fails, so a deployment that dies
half way still leaves a report that says how far it got and a per-node
transcript of every command.

### Cross-wiring

Cross-wiring goes through zodan's `spock.add_node` rather than raw
`node_create`/`sub_create`. `add_node` is cluster-aware — it discovers every
node already registered with the source and wires the newcomer to all of them in
both directions, with the sync-event handshakes that make joining a *live*
cluster safe. So adding the Nth node is always one call against node 1:

```
n1                       first node — nothing to wire
add_node(n1 -> n2)       n1 <-> n2
add_node(n1 -> n3)       n1 <-> n3, and n2 <-> n3
add_node(n1 -> n4)       ... and so on
```

`add_node` exits zero even when individual phases fail, so its
`Success rate: %100` line is checked explicitly. See
[`configuration/spock/README.md`](configuration/spock/README.md) for which zodan
revision goes with which Spock major, and why it matters.

### Passwordless psql

After a deployment, `psql` works from any host to any node without a password,
while the wire still uses `scram-sha-256` rather than `trust`:

```bash
psql -h 10.0.1.11 -p 5432 -U postgres -d postgres
psql service=n2          # named connections from pg_service.conf
```

`.pgpass` carries **every** node, not just the local one, because zodan's
cross-wiring and the health collector both connect outward from whichever node
they run on. `pg_hba` lists each node's address explicitly rather than opening a
wide CIDR, so the rule set stays as narrow as the cluster is.

---

## Commands

### Deploy

```bash
./pg_deploy_cluster.sh                                    # interactive
./pg_deploy_cluster.sh --nodes 3 --pg-major 17 --standby n1,n2
./pg_deploy_cluster.sh --mode source --pg-version 17.11
./pg_deploy_cluster.sh --clean --nodes 2                  # wipe a previous run first
./pg_deploy_cluster.sh --dry-run --nodes 4                # plan only
./pg_deploy_cluster.sh --help
```

Passing **any** flag turns off the prompts, which is what CI should do.

### Status

```bash
./pg_cluster_status.sh                            # most recent cluster
./pg_cluster_status.sh --cluster demo
./pg_cluster_status.sh --cluster demo --json      # machine-readable
./pg_cluster_status.sh --cluster demo --report    # also write an HTML report
./pg_cluster_status.sh --list                     # what has been deployed
```

Exits non-zero when the cluster is not fully healthy, so it works directly as a
monitoring or CI check.

### Add a standby later

```bash
python3 -m deployment.cli add-standby --cluster demo --leader n2
python3 -m deployment.cli add-standby --cluster demo --leader n2 --host node-c
```

This is more than starting another Patroni instance: the new member's permanent
replication slot is added to the **live** DCS configuration (a slot declared in
the leader's `bootstrap` block would never be created, because bootstrap already
ran), and `.pgpass` and `pg_service.conf` on every host learn the new node.

### Tear down

```bash
python3 -m deployment.cli remove --cluster demo             # data, configs, etcd state
python3 -m deployment.cli remove --cluster demo --purge     # also uninstall packages
```

`remove` stops Patroni before touching anything, so no surviving member tries to
fail over into a data directory that is being deleted. Hosts that cannot be
reached are reported and left untouched rather than aborting the teardown.

---

## Dashboard

```bash
./pg_dashboard.sh                              # http://127.0.0.1:8080
./pg_dashboard.sh --port 9000 --interval 10 --cluster demo
```

Shows every node's Patroni role, whether it accepts writes, its Spock
subscription count, replication slots and retained WAL, plus etcd health and
per-host load, memory and disk.

Health collection walks every host over SSH, which takes seconds — far too slow
for a request. A background poller refreshes on the interval and the web layer
only ever serves the cached snapshot, so a browser refresh never blocks on SSH
and ten open tabs cost the same as one. A collection that fails after a good one
shows the last known state and says the refresh is failing, rather than blanking
the page.

> **No authentication.** Binding to `0.0.0.0` exposes your cluster topology and
> health to anyone who can reach the port. Leave it on localhost and use an SSH
> tunnel, or put it behind something that authenticates.

---

## Reports and logs

Every run writes `reports/runs/<run-id>/`:

| File | Contents |
|---|---|
| `report.html` | Self-contained page — no CDN, opens from a `file://` URL |
| `report.json` | The same data, for CI to assert on |
| `summary.txt` | The terminal recap, for when the run has scrolled away |

`reports/index.html` lists every run, newest first, and rebuilds itself — a
history of deployments accumulates with nobody maintaining it.

Logs live in `logs/<run-id>/`: `deploy.log` is the full transcript,
`<node>.log` is every command and its output for that node (this is where
zodan's very verbose cross-wiring output goes), and `steps.json` is the step
timeline, written incrementally so a run that dies still leaves a usable record.

Values that arrive from remote hosts — node names, package versions, command
output — are HTML-escaped, so nothing a host emits can inject markup into a page
you open. Passwords and SSH key paths are never written to a report, a state
file, or the dashboard API.

---

## Operational notes

### After a Patroni failover, retarget the peers

This is the one manual step, and it is inherent to combining Spock with Patroni
rather than a gap in the tooling.

When Patroni promotes `n1s1`, the Spock node identity `n1` stays the same but it
now answers on a **different address and port**. `n1`'s peers still hold the DSN
they were given at cross-wire time, so they keep replicating from an address
that is now a read-only replica. Spock has no VIP concept, so somebody has to
move them:

```bash
./pg_cluster_status.sh --cluster demo --repair-failover n1
```

That disables each peer's subscription to `n1`, adds an interface pointing at the
promoted address, switches the subscription to it and re-enables it. The status
output and the dashboard both flag the condition when they see a Spock node
reporting a non-leader Patroni role, so you are told before you have to go
looking.

### Version pins

`configuration/config<major>.env` records the versions a deployment expects.
These are reference points, not gates — the deployment installs whatever the
selected channel offers and records the result, so a report can answer "was this
run the same as last week's?". Change what gets installed with `--channel` or a
source build, not by editing a pin.

### output_plugin_libraries

PostgreSQL 16.15 / 17.11 / 18.5 / 19.0beta3 began requiring logical decoding
output plugins to be allow-listed before the server will load them. Spock's
`spock_output` is not in the default list, so on those releases every
cross-wired subscription fails until the GUC is set — and *older* point releases
refuse to start when it is present. The GUC is therefore gated on the server's
actual version, never applied unconditionally.

---

## Troubleshooting

**`sudo: a password is required`** — the inventory user needs passwordless sudo.
Every command runs through `sudo -n`.

**A node never becomes leader** — read `logs/<run-id>/<node>.log`, then
`journalctl -u patroni-<node>` on the host. The usual cause on a redeploy is
stale DCS state from a previous cluster with the same scope name; `--clean`
clears it.

**etcd has no quorum** — members must reach each other on 2380 and clients on
2379. Check the security group or firewall between hosts.

**Cross-wiring reports less than 100%** — the full zodan output is in
`logs/<run-id>/<node>.log`. A common cause is `dblink` missing, which means the
`contrib` package did not install.

**`Could not get lock /var/lib/dpkg/lock`** — handled: background apt timers are
stopped and the locks waited out during prerequisites. If it still appears,
something outside the deployment is holding dpkg.

---

## License

[The PostgreSQL License](LICENSE). 
