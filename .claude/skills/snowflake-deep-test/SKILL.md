---
name: snowflake-deep-test
description: |
  Test Snowflake sequences on the cluster this repository has deployed, in
  depth and from the laptop: node identity in every id, uniqueness under
  concurrent inserts from every node, monotonicity within a node and
  ordering across nodes, the counter under a tight loop, converting
  existing sequences and the tables that use them, ids as primary keys
  replicating through Spock, behaviour on a promoted standby, decoding
  ids back to time, counter and node, and a full restore. Use when asked
  to test, exercise, stress or find bugs in snowflake, sequences, id
  generation or duplicate keys on the running cluster or "my cluster".
  Needs the snowflake extension in the database on every Spock node. Do
  not run it together with failover-deep-test, or on a production cluster.
---

# Snowflake, in depth, on the deployed cluster

Input: optional cluster name (default: the most recently deployed), optional
list of scenarios to skip or focus on.

Goal: show that ids minted on different nodes at the same instant never
collide and still replicate and sort sensibly, which is the whole reason
Snowflake is in a multi-master cluster, and find where it stops holding:
tight loops, converted sequences, a standby that takes over, clock
differences. Every oddity goes into one of three bins: a bug in Snowflake,
a bug in this tool, or a limitation of the topology. The cluster is the
harness; drop what you create.

Everything runs through this repository's scripts. Never `pg_ctl`, never
`systemctl`, never a config edit by hand. Do not edit any file in the
repository while testing; findings go in the report. Not at the same time
as `failover-deep-test`.

## 0. Read the plan before touching anything

`configuration/clusters/<name>.json` holds the plan. Write down, in node
names only: the Spock nodes and their standbys, `pg_extensions` (snowflake
must be listed or created by hand on every node; `spock sequences` says
whether it is available on each), the Spock major and PostgreSQL version
per node, and whether every node shares one host (then there is no clock
skew to observe; say so rather than claim you tested it).

Model to keep in mind: a Snowflake id is a 64-bit integer made of a
millisecond timestamp, a per-millisecond counter and the node id
`snowflake.node` (1 to 1023), which must differ per Spock node, comes from
the number in the node's name, and is unset by default so that
`snowflake.nextval()` raises until it is configured. A standby is a byte
copy of its leader and carries the leader's id, which is right because it
serves the same Spock node once promoted. `snowflake.nextval(regclass)`
takes a normal sequence and returns a Snowflake id instead of the
sequence's own value; `spock sequence-convert` rewrites a table's defaults
to do that. The upstream harness (`--health_check snowflake`) drives two
nodes through its own cases; this skill starts where it stops.

## 1. The tools

Activate the virtualenv first (`source venv/bin/activate`).

- `./pg_cluster_ctl.sh node command '<SQL>' --sql --on n1,n2,n3` (also
  `--compare`, `--on all`; without `--sql` a shell command on the host).
- `./pg_cluster_ctl.sh spock sequences` (every sequence, and whether
  Snowflake is available per node), `spock sequence-convert` (`--help`
  for its arguments), `spock sub-show-status | lag | sub-wait-for-sync`,
  `./pg_cluster_ctl.sh diff table <schema.table>`.
- `./pg_deploy_cluster.sh --health_check snowflake` for the upstream
  harness, once at the start and once at the end.
- SQL: `SHOW snowflake.node`; `SELECT snowflake.nextval()` and
  `snowflake.nextval('seq'::regclass)`; `snowflake.currval(...)`;
  `snowflake.format(id)` (and any `snowflake.id_*` helpers `\df snowflake.*`
  lists) to decode an id into timestamp, counter and node; `SELECT (id >>
  ...)` arithmetic from the extension's README when no helper exists.
- A tight loop: `INSERT INTO t (id) SELECT snowflake.nextval() FROM
  generate_series(1, 100000)` in one statement, and
  `pgbench -n -c 8 -T 30 -f <script>` through `app run`-style shell
  commands on the host with a custom script that inserts one row.

Practical: put everything in a `deep_snowflake` schema created through
`spock.replicate_ddl`, add tables to the default repset, and wait for
replication by polling counts on the target for up to sixty seconds.
Longer loops go in the background with a generous timeout; serialize SSH
commands against one host.

## 2. Scenarios

Record for each: the commands, the key output, PASS / FAIL / NOT RUN with
the reason, and timings. Stop at the first scenario whose cleanup you
cannot complete.

1. **Baseline.** `--health_check snowflake` passing; `snowflake.node`
   set and different on every Spock node, equal to the leader's on each
   standby, matching the number in the node's name; `spock sequences`
   shows Snowflake available everywhere; subscriptions replicating.
2. **Identity in every id.** Ten ids from each node, decoded: the node
   field equals that node's `snowflake.node`, the timestamp is within a
   few seconds of `now()` on that node, the counter starts at zero per
   millisecond. Record the decode you used so the report is reproducible.
3. **Uniqueness under concurrent inserts.** A table with a `bigint`
   primary key defaulting to `snowflake.nextval()`, in the default
   repset. Insert fifty thousand rows from every Spock node at the same
   time (background, one statement per node). When all finish and
   replicate: `count(*)` equals the total on every node, `count(DISTINCT
   id)` equals `count(*)`, no apply errors in `spock.sub_show_status()`
   or the logs, `diff table` clean. Time the convergence.
4. **Monotonicity and ordering.** Within one node, ids from one session
   strictly increase; across nodes, ids sorted by value are sorted by
   their embedded timestamp, and two ids from different nodes in the
   same millisecond differ only in the node field. On a single host,
   say that clock skew could not be observed.
5. **The counter in a tight loop.** One million `nextval()` calls in one
   statement on one node: no duplicates, and either the counter wraps
   within a millisecond into the next millisecond's space or the call
   waits; report which, with the throughput (ids per second) you
   measured, and whether the extension's README documents the behaviour.
6. **Converting an existing table.** A table with a `serial` or
   `identity` column and a few rows, replicated. `spock sequence-convert`
   on it (or the extension's own conversion function). New inserts on
   every node mint Snowflake ids, old rows keep theirs, the ids are
   larger than every old value, no collision, and the default replicates
   as DDL so every node's new rows use Snowflake (check `\d` on every
   node with `--compare`).
7. **Sequences not converted.** A plain `serial` table inserted on two
   nodes at once: show the duplicate-key apply error that Snowflake
   exists to prevent, how the subscription reacts (`exception_log`,
   `exception_behaviour`), and recover it (`sub-resync-table` or delete
   the offending row). File it as the expected baseline, not a bug.
8. **A promoted standby mints ids** (only when a Spock node has a
   standby, and never while `failover-deep-test` runs). Use
   `./pg_cluster_ctl.sh service switchover --node <subject> --candidate
   <standby>` (it refuses a candidate without slot copies; wait and retry
   rather than `--force`). On the promoted standby, `snowflake.node`
   equals the subject's id, ids it mints carry that node field, inserts
   replicate to the peers after `./pg_cluster_status.sh
   --repair-failover <subject>`, and no id collides with those the
   subject minted before. Switch back the same way and repair again.
9. **Types and edges.** `nextval()` into an `integer` column fails as it
   should (ids are 64-bit); ids compared as text sort wrongly (document
   it as expected); `snowflake.format` of an id minted a year in the
   past, if the extension lets you build one, decodes back.
10. **Logs.** `service logs --component postgres` on every Spock node:
    every ERROR, FATAL, PANIC and conflict line, each marked expected or
    not.
11. **Restore and verify.** `DROP SCHEMA deep_snowflake CASCADE` through
    `replicate_ddl`, zero `deep_snowflake` objects in `pg_namespace` and
    `spock.tables` on every node, any converted table dropped,
    subscriptions replicating, lag zero, the scope back on its subject if
    scenario 8 ran, `--health_check snowflake` passing. Anything you
    could not restore goes at the top of the report.

## 3. The report

Write it to the scratchpad as `snowflake-deep-test-report.md`, and
summarize it in the final message. Node names only: never a host address,
hostname, username, key path or password; redact the address from any
status output you quote. Per scenario: what was done, what happened, the
verdict, the timings, throughput numbers in a table. End with a ranked
list of findings, each labelled as one of **bug in Snowflake** (version,
PostgreSQL version of the node, exact observation), **bug in Spock**,
**bug in postgres-cluster-deployment** (file and behavior), or
**topology limitation** (single host, no clock skew observable).

Report faithfully. A step that hung or failed is reported with its output,
never guessed at.
