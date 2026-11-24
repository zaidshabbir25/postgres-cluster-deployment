---
name: lolor-deep-test
description: |
  Test lolor (replicating large objects) on the cluster this repository
  has deployed, in depth and from the laptop: node identity in the OID
  bits, the whole large-object API through lolor on every node, streaming
  reads and writes, big and many objects, concurrent creation on two nodes
  without collisions, concurrent updates and unlink races, the drop guard,
  import and export, cleanup after unlink, and a full restore. Use when
  asked to test, exercise, stress or find bugs in lolor, large objects or
  blobs on the running cluster or "my cluster". Needs the lolor extension
  in the database on every Spock node. Do not run it together with
  failover-deep-test, or on a production cluster.
---

# lolor, in depth, on the deployed cluster

Input: optional cluster name (default: the most recently deployed), optional
list of scenarios to skip or focus on.

Goal: show that large objects really travel between the nodes, through the
standard API, under the conditions that break them: created at the same
moment on two nodes, changed on two nodes, unlinked while being written,
big enough to span many chunks, numerous enough to matter. Every oddity
goes into one of three bins: a bug in lolor or Spock, a bug in this tool,
or a limitation of the topology. The cluster is the harness; unlink every
object you create and drop every schema.

Everything runs through this repository's scripts. Never `pg_ctl`, never
`systemctl`, never a config edit by hand. Do not edit any file in the
repository while testing; findings go in the report. Not at the same time
as `failover-deep-test` or `--health_check failover`.

## 0. Read the plan before touching anything

`configuration/clusters/<name>.json` holds the plan. Write down, in node
names only: the Spock nodes and their standbys, `pg_extensions` (lolor
must be listed, or `CREATE EXTENSION` must have been done by hand on every
node: `--health_check lolor` reports "not installed" otherwise and so do
you), the Spock major and PostgreSQL version per node.

Model to keep in mind: lolor replaces the server's large-object storage
with its own tables, `lolor.pg_largeobject` and
`lolor.pg_largeobject_metadata`, which sit in a replication set like any
other table, so `lo_*` calls replicate through Spock as row changes. The
catalog `pg_catalog.pg_largeobject` stays empty. Each node generates OIDs
whose four low bits are its `lolor.node` (1 to 15), so two nodes never
mint the same OID, and that is why the GUC must differ per node and why a
cluster tops out at fifteen nodes. lolor is in `shared_preload_libraries`
because its drop guard is an object access hook. A standby is a byte copy
of its leader and carries the leader's id, which is correct. The in-tree
`lolor` suite (`aspects/health_check/suites/lolor/sql/`) already checks
the extension, the identity, the repset membership and one
create/update/unlink round trip; this skill starts where it stops.

## 1. The tools

Activate the virtualenv first (`source venv/bin/activate`).

- `./pg_cluster_ctl.sh node command '<SQL>' --sql --on n1,n2,n3` (also
  `--compare`, `--on all`; without `--sql` a shell command on the host).
- `./pg_cluster_ctl.sh db guc-show --pattern 'lolor%' --compare`-style
  checks via `node command 'SHOW lolor.node' --sql --compare` (the values
  must differ, so a `--compare` that reports a difference is the PASS).
- `./pg_cluster_ctl.sh spock sub-show-status | lag | repset-list-tables |
  sub-wait-for-sync`, `./pg_cluster_ctl.sh diff table
  lolor.pg_largeobject` and `diff table lolor.pg_largeobject_metadata`.
- `./pg_deploy_cluster.sh --health_check lolor` for the basics, once at the
  start and once at the end; `--health_check lolor-upstream` runs lolor's
  own pg_regress in a scratch instance (library only, not the mesh).
- SQL: `lo_create(0)`, `lo_from_bytea(0, bytea)`, `lo_put(oid, offset,
  bytea)`, `lo_get(oid)`, `lo_get(oid, offset, length)`, `lo_unlink(oid)`,
  `lo_open/loread/lowrite/lo_lseek64/lo_tell64/lo_truncate64/lo_close`
  inside one transaction, `lo_import`/`lo_export` with a server-side path
  under the data directory's parent, `SELECT count(*), sum(length(data))
  FROM lolor.pg_largeobject WHERE loid = X`, `SELECT loid & 15`.
- Shell on a host: `dd if=/dev/urandom of=/tmp/deep_lolor.bin bs=1M
  count=64` for an import source; `md5sum` to compare an export with it.

Practical: keep every OID you mint in a table `deep_lolor.objects (loid
oid, node text)` created through `spock.replicate_ddl`, so cleanup is one
loop of `lo_unlink` plus a `DROP SCHEMA`. Wait for replication by polling
`EXISTS (SELECT 1 FROM lolor.pg_largeobject WHERE loid = X)` on the target
for up to sixty seconds, never with a fixed sleep. Large transfers go in
the background with a generous timeout; serialize SSH commands against one
host.

## 2. Scenarios

Record for each: the commands, the key output, PASS / FAIL / NOT RUN with
the reason, and timings. Stop at the first scenario whose cleanup you
cannot complete.

1. **Baseline.** `--health_check lolor` passing; `lolor.node` differs on
   every Spock node and equals the leader's on each standby;
   `shared_preload_libraries` contains lolor everywhere; both lolor tables
   are in a replication set on every node (`repset-list-tables`); the
   catalog `pg_largeobject` is empty everywhere; subscriptions
   replicating; `diff table` on both lolor tables clean.
2. **The whole API, from every node.** On each Spock node in turn:
   `lo_create`, `lo_put` at offset 0 and at an offset past the end,
   `lo_get` whole and partial, `lo_truncate64` to a shorter and a longer
   length, a `lo_open`/`lowrite`/`lo_lseek64`/`loread`/`lo_close` session
   in one transaction, then `lo_unlink`. After every step, the same
   `lo_get` on every peer returns the same bytes (compare `md5(lo_get(X))`
   with `--compare`), and the OID's low bits name the node that minted it.
3. **Big objects.** A 64 MiB object built on n1 with `lo_from_bytea` of
   `repeat`ed random bytes, or by `lo_import` of a file made with `dd`;
   time until `sum(length(data))` matches on every peer; `md5(lo_get)`
   equal everywhere; chunk count in `lolor.pg_largeobject` equal
   everywhere. Then `lo_export` on a peer and `md5sum` against the source
   file. Note the chunk size lolor uses and how many rows one object
   became.
4. **Many objects.** One thousand small objects in one transaction from
   n2, then one thousand each from two nodes at the same time: no OID
   appears twice across `lolor.pg_largeobject_metadata` on any node,
   every object is on every node, and `diff table` on both lolor tables
   is clean. Time the convergence.
5. **Concurrent updates to one object.** Pause the subscriptions between
   n1 and n2, `lo_put` different bytes at the same offset on both, resume
   with immediate, wait. The last commit timestamp wins, the same bytes
   end up on every node, and `spock.resolutions` records the decision.
   Then an update on one node racing an unlink on another: the object
   must be either absent everywhere or whole everywhere, never a metadata
   row without data or data without metadata (check both tables).
6. **Partial writes and transactions.** `lo_put` inside a transaction that
   rolls back: nothing replicates. A `lowrite` session that writes ten
   chunks and commits: all ten arrive in one apply, never a peer showing
   five. A savepoint rolled back inside the session.
7. **The drop guard.** Try to `DROP EXTENSION lolor` and to drop or
   truncate `lolor.pg_largeobject` while objects exist: expect refusal,
   note the message. Confirm nothing replicated from the attempt.
8. **Unlink and space.** Unlink half the objects from scenario 4 on n3:
   the rows vanish from both lolor tables on every node; `lo_get` on a
   peer raises "does not exist"; after `VACUUM lolor.pg_largeobject` the
   table size on each node drops (note it does not have to drop
   identically).
9. **Through the mesh with a peer down.** Stop Patroni on a Spock node
   without a standby, create and update objects on the others for a
   minute, start it: everything arrives, `md5` equal, under two minutes
   down.
10. **A standby's view.** On a standby, `lo_get` of an object the leader
    just wrote returns the bytes (physical replication carries lolor's
    tables like any other), and `lolor.node` on the standby equals the
    leader's.
11. **Logs.** `service logs --component postgres` on every Spock node:
    every ERROR, FATAL, PANIC and conflict line, each marked expected or
    not.
12. **Restore and verify.** Every OID in `deep_lolor.objects` unlinked
    on the node that owns it, the schema dropped through `replicate_ddl`,
    `lolor.pg_largeobject` and its metadata empty of anything you made on
    every node (`diff table` clean), import files removed from the host,
    subscriptions replicating, lag zero, `--health_check lolor` passing.
    Anything you could not restore goes at the top of the report.

## 3. The report

Write it to the scratchpad as `lolor-deep-test-report.md`, and summarize
it in the final message. Node names only: never a host address, hostname,
username, key path or password; redact the address from any status output
you quote. Per scenario: what was done, what happened, the verdict, the
timings. End with a ranked list of findings, each labelled as one of
**bug in lolor** (version, PostgreSQL version of the node, exact
observation), **bug in Spock** (when the rows replicated wrongly rather
than lolor storing them wrongly), **bug in postgres-cluster-deployment**
(file and behavior), or **topology limitation**.

Report faithfully. A step that hung or failed is reported with its output,
never guessed at.
