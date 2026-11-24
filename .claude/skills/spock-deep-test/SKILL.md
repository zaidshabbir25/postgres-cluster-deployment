---
name: spock-deep-test
description: |
  Test Spock multi-master replication of the cluster this repository has
  deployed, in depth and from the laptop: DDL and DML in every direction,
  the conflict cases (update/update, insert/insert, update/delete,
  delete/delete) and their resolution, delta-apply counters, Snowflake
  sequences, no-primary-key and partitioned tables, apply-side exceptions
  and recovery, a peer down while the others write, concurrent pgbench on
  every node, replication-set filtering, repair mode, read-only mode,
  table resync, and a full restore. Use when asked to test, exercise,
  stress or find bugs in Spock, replication, MMR, conflicts, DDL
  replication or the mesh on the running cluster or "my cluster". It
  stops a peer briefly and writes conflicting rows on purpose. Do not run
  it together with failover-deep-test, or on a production cluster.
---

# Spock multi-master, in depth, on the deployed cluster

Input: optional cluster name (default: the most recently deployed), optional
list of scenarios to skip or focus on.

Goal: find out whether this mesh really converges, in every direction and
under the conditions that break multi-master systems: concurrent writes to
the same row, DDL from a node that is not the one the operator is looking
at, a peer that is down while the others write, an apply error on one side.
Every oddity goes into one of three bins: a bug in Spock, a bug in this
tool, or a limitation of the topology. The cluster is the harness; drop
what you create and leave every subscription replicating.

Everything runs through this repository's scripts. Never `pg_ctl`, never
`systemctl`, never `spock rebuild`, never an edit to a config file by hand.
Do not edit any file in the repository while testing; findings go in the
report. Do not run this while `failover-deep-test` or `--health_check
failover` is running: both change the cluster.

## 0. Read the plan before touching anything

`configuration/clusters/<name>.json` holds the plan. Write down, in node
names only:

- the Spock nodes (`role: spock`) and their standbys. Standbys replicate
  physically and take no part here except as something that must stay
  streaming while you work; writes go to Spock nodes only.
- the subscriptions: every Spock node holds `sub_<provider>_<self>` for
  each peer, so N nodes carry N×(N−1) subscriptions. Confirm the count
  with `spock sub-list`.
- `pg_extensions` (lolor, snowflake): scenarios 6 and 13 run only when the
  extension is installed.
- `spock_major` and the PostgreSQL version per node. A mixed-version
  cluster is a feature of the test; note which node runs what, since
  behaviour that differs by node often differs by version.
- `synchronous_mode_strict`: a scope whose standby is down refuses writes.
  Nothing here stops a standby, but if a write hangs, check that first.

Model to keep in mind: Spock is asynchronous logical replication in both
directions between every pair of nodes. Each write is applied on the peers
by an apply worker, with the row's commit timestamp deciding conflicts
(last update wins) unless a column uses delta-apply. DDL replicates only
through `spock.replicate_ddl` or, when `spock.enable_ddl_replication` is
on, automatically. A table replicates only once it is in a replication set;
tables without a primary key go to `default_insert_only` and only their
INSERTs replicate. The in-tree `spock` suite under
`aspects/health_check/suites/spock/sql/` already covers the basics (extension,
nodes, subscriptions, slots, one DDL, one DML, one bidirectional case); this
skill starts where it stops.

## 1. The tools

Activate the virtualenv first (`source venv/bin/activate`).

- `./pg_cluster_status.sh [--json]`: health, slots, lag.
- `./pg_cluster_ctl.sh node command '<SQL>' --sql --on n1,n2,n3` (also
  `--compare`, which diffs the output across nodes, and `--on all`;
  without `--sql` it is a shell command on the host). One statement per
  command where a function refuses a transaction block.
- `./pg_cluster_ctl.sh spock node-list | sub-list | repset-list |
  repset-list-tables | sub-show-status | sub-show-table | lag | version |
  sequences | no-primary-key | repset-create | repset-drop |
  repset-add-table | repset-remove-table | sub-add-repset |
  sub-remove-repset | sub-enable | sub-disable | sub-wait-for-sync |
  sub-resync-table | replicate-ddl | sequence-convert` (`--help` on each).
- `./pg_cluster_ctl.sh db guc-show --pattern 'spock%'`, `db guc-set`,
  `db guc-reset`, `db set-readonly on|off --node X`, `db tables`.
- `./pg_cluster_ctl.sh app install --app pgbench --scale 5`, `app run`,
  `app counts`, `app concurrent-index`, `app remove`.
- `./pg_cluster_ctl.sh diff all | table <schema.table> | repair <table>
  --source <node>`.
- `./pg_cluster_ctl.sh service stop | start | restart --node X
  --component patroni|postgres`, `service logs --node X --component
  postgres --lines N`.
- `./pg_deploy_cluster.sh --health_check spock` runs the in-tree basics;
  worth one run at the start and one at the end.
- SQL worth repeating: `SELECT * FROM spock.sub_show_status()`;
  `SELECT * FROM spock.tables` (which repset holds which table);
  `SELECT * FROM spock.resolutions ORDER BY id DESC LIMIT 20` (conflict
  decisions; the name of the conflict table and the exception log,
  `spock.exception_log`, vary a little by version, so `\dt spock.*` first);
  `SHOW spock.conflict_resolution`, `SHOW spock.enable_ddl_replication`,
  `SHOW spock.include_ddl_repset`, `SHOW spock.exception_behaviour`,
  `SHOW track_commit_timestamp`; `SELECT pg_current_wal_lsn()`;
  `SELECT slot_name, active, pg_wal_lsn_diff(pg_current_wal_lsn(),
  confirmed_flush_lsn) AS behind FROM pg_replication_slots`.

Practical: give every object you create the prefix `deep_` in a schema of
its own, so cleanup is one `DROP SCHEMA ... CASCADE` through
`replicate_ddl` plus a check that it went. Wait for replication with
`spock sub-wait-for-sync` or by polling a count on the target for up to
sixty seconds, never with a fixed sleep. Run anything longer than a minute
in the background with a generous timeout, and serialize SSH commands
against one host rather than fanning them out.

## 2. Scenarios

Record for each: the commands, the key output, PASS / FAIL / NOT RUN with
the reason, and timings where a wait was involved. Stop at the first
scenario whose cleanup you cannot complete.

1. **Baseline.** status, node-list, sub-list, sub-show-status (every
   subscription replicating), lag, spock version on every node (same
   build everywhere, note rc suffixes), the Spock GUCs on every node with
   `--compare`, `track_commit_timestamp` on everywhere, diff all clean,
   `--health_check spock` passing. Everything healthy, or stop and report.
2. **DDL from every node.** With `enable_ddl_replication` as deployed:
   CREATE SCHEMA and a table with a primary key from n1, another from n2,
   another from the last node, each through `replicate_ddl` (and once
   plainly, to see whether auto DDL picks it up and puts the table in a
   repset: check `spock.tables` on every node). Then ALTER TABLE ADD
   COLUMN with a default, CREATE INDEX, ALTER COLUMN TYPE, RENAME, DROP
   COLUMN, each from a different node, and confirm the schema matches
   everywhere with `diff schema`.
3. **DML in every direction.** From each node: INSERT a batch, UPDATE a
   batch, DELETE a batch, in that order, on rows the other nodes wrote.
   Round-trip check: a row inserted on n1, updated on n2, deleted on n3
   ends absent everywhere. `diff all` clean.
4. **Conflicts.** On one row, a concurrent UPDATE from two nodes (disable
   the subscriptions between them first, write on both, re-enable with
   immediate, then wait): last commit timestamp must win on every node,
   the same winner everywhere, and `spock.resolutions` must record the
   decision on the loser's side. Repeat for INSERT/INSERT with the same
   key, UPDATE/DELETE, and DELETE/DELETE. After each, `diff table` on that
   table must be clean. A row that differs between nodes after the waits
   is the headline finding.
5. **Delta-apply counters.** A table with a numeric column set to
   `LOG_OLD_VALUE=true, DELTA_APPLY_FUNCTION=spock.delta_apply` (check the
   syntax in this Spock version's README if ALTER TABLE refuses it).
   Concurrent `+1` loops on two nodes (one hundred each). The final value
   must be the sum, not either node's count, on every node.
6. **Sequences and Snowflake** (only with snowflake installed). `spock
   sequences`, a table with a `nextval` default on each node, concurrent
   inserts from two nodes, no duplicate keys anywhere. Without snowflake,
   show the collision and file it as a topology limitation.
7. **Tables Spock treats differently.** A table without a primary key
   (goes to `default_insert_only`; `spock no-primary-key` lists it; its
   UPDATEs must not replicate and that is expected), a partitioned table
   with two partitions (`spock.replicate_ddl`, rows into both partitions,
   `spock.tables` shows what was added), a table with a generated column,
   a TRUNCATE from one node (replicates by default in Spock 5: confirm).
8. **Large transactions and load.** One transaction of two hundred
   thousand rows from n1; time until the last row is on every peer; lag
   during and after. Then `app install --app pgbench` and `app run` on
   every Spock node at the same time for sixty seconds, so the same
   pgbench rows are hit from every node. When all runs end, wait for lag
   zero and run `diff table` on the four pgbench tables: they must
   converge, and `spock.resolutions` tells how many conflicts it took.
   Anything that does not converge is a finding.
9. **A peer goes away while the others write.** Stop Patroni on one Spock
   node (one without a standby, so no promotion happens). Write on the
   remaining nodes for about a minute; note the slot for the absent peer
   accumulating WAL in `spock lag`. Start it again: time until its
   subscriptions report replicating and the rows it missed are there; lag
   back to zero. Keep it down under two minutes.
10. **Apply-side errors and recovery.** Make the apply fail on one node
    only: for example a UNIQUE index on a secondary column that exists on
    n2 alone, then insert on n1 a row that violates it. Observe the
    subscription on n2 (down? retrying?), `spock.exception_log`, and
    whether `spock.exception_behaviour` discards the transaction or stops
    the subscription. Fix the cause, re-enable, confirm replication
    resumes and the rest of the backlog arrives in order.
11. **Replication-set filtering.** `repset-create deep_set`, add one table
    to it only, `sub-add-repset` on one subscription only: rows reach that
    subscriber and no other. `sub-remove-repset`, `repset-drop`, back to
    the baseline.
12. **Repair mode and read-only.** In one session on n1,
    `SELECT spock.repair_mode(true)` then an INSERT: the row must stay
    local. `db set-readonly on --node n2`: a write on n2 must be refused,
    replication into n2 must still apply (Spock bypasses the read-only
    GUC: confirm). `set-readonly off`.
13. **Resync.** Delete rows on n2 in repair mode so n2 diverges, confirm
    `diff table` reports it, then `spock sub-resync-table` (or
    `diff repair --source n1`) and confirm it is clean again. With lolor
    installed, store and read back a large object through it on two
    nodes.
14. **Apply worker restart under backlog.** Disable one subscription,
    write ten thousand rows on its provider, `service restart --component
    postgres` on the subscriber, enable the subscription with immediate:
    everything arrives, once, in order, no duplicates.
15. **Logs.** `service logs --component postgres` on every Spock node:
    list every ERROR, FATAL, PANIC and every conflict line, and say which
    were expected from the scenarios above.
16. **Restore and verify.** Every `deep_` object, repset, extra index and
    pgbench dropped on every node (`replicate_ddl` plus a count of
    `deep_%` in `pg_namespace` and `spock.tables` on every node),
    subscriptions all replicating, lag zero, GUCs back to what step 1
    recorded, read-only off, diff all clean, `--health_check spock`
    passing, status exits zero. Anything you could not restore goes at
    the top of the report.

## 3. The report

Write it to the scratchpad as `spock-deep-test-report.md`, and summarize
it in the final message. Node names only: never a host address, hostname,
username, key path or password; the status output prints the address, so
redact it. Per scenario: what was done, what happened, the verdict, the
timings. End with a ranked list of findings, each labelled as one of:

- **bug in Spock**, with the version, the node's PostgreSQL version, and
  the exact observation;
- **bug in postgres-cluster-deployment**, with the file and the behavior;
- **topology limitation** (no Snowflake, single host, mixed versions),
  worth knowing but not fixing here.

Report faithfully. A step that hung or failed is reported with its output,
never guessed at.
