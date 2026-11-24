---
name: ace-deep-test
description: |
  Test ACE, the Active Consistency Engine, against the cluster this
  repository has deployed, in depth and from the laptop: both the
  installed ace binary on the host and this tool's own `diff` commands.
  Plants every kind of divergence on purpose (changed, missing and extra
  rows, NULLs, awkward types, schema drift, replication-set drift, extra
  Spock interfaces), checks each is detected, scoped with filters and
  buckets, repaired from a chosen source with and without truncation,
  re-run from a saved diff, and that a clean cluster stays clean. Use
  when asked to test, exercise, stress or find bugs in ace, table-diff,
  table-repair, consistency checks or divergence on the running cluster
  or "my cluster". It writes with replication off on purpose. Do not run
  it together with failover-deep-test, or on a production cluster.
---

# ACE, in depth, on the deployed cluster

Input: optional cluster name (default: the most recently deployed), optional
list of scenarios to skip or focus on.

Goal: find out whether the consistency tooling tells the truth about this
mesh: does it see every way the nodes can disagree, does it stay quiet when
they agree, and does its repair put them back without making it worse. Two
implementations are under test, the `ace` binary installed on the host
(`pgedge-ace`) and this repository's `pg_cluster_ctl.sh diff`
(`aspects/consistency.py`, bucketed checksums with drill-down); where they
disagree with each other, that is a finding in itself. Every oddity goes
into one of three bins: a bug in ACE, a bug in this tool, or a topology
limitation. The cluster is the harness; every divergence you plant is
repaired or dropped before you finish.

Everything runs through this repository's scripts, plus the `ace` binary
on the host through `node command` without `--sql`. Never `pg_ctl`, never
`systemctl`, never a config edit by hand. Do not edit any file in the
repository while testing; findings go in the report. Not at the same time
as `failover-deep-test`, since a promotion changes the interfaces ACE's
spock-diff compares.

## 0. Read the plan before touching anything

`configuration/clusters/<name>.json` holds the plan. Write down, in node
names only: the Spock nodes (ACE compares those; standbys are not
members), the PostgreSQL version per node (a mixed cluster makes
schema-diff and type handling more interesting; note it), and whether
`pgedge-ace` is installed on the node you run from (`--health_check ace`
reports "the ace binary" missing otherwise, and then only this tool's
`diff` is under test).

Model to keep in mind: a divergence is only possible when a write does not
replicate, so you plant one by writing inside `SELECT
spock.repair_mode(true)` in the same session, which keeps the change local.
Rows then differ on one node only; the other nodes stay the reference.
Detection compares nodes pairwise on primary-key order, so a table without
a primary key cannot be compared, and both implementations must say so
rather than guess. `--health_check ace` runs `spock-diff`, `schema-diff
public` and `repset-diff default` from a workspace holding ACE's
`ace.yaml` and a service file for every node; the in-tree work there
covers the metadata halves, and this skill starts where it stops.

## 1. The tools

Activate the virtualenv first (`source venv/bin/activate`).

- This tool: `./pg_cluster_ctl.sh diff all | spock | repset | schema |
  table <schema.table> [--buckets N] [--where '<predicate>']
  [--no-drill-down] [--json]`, `diff repair <schema.table> --source <node>
  [--targets n2,n3] [--no-truncate] --yes`. Exit code 0 means agreement,
  1 means a difference was found.
- ACE on the host, as the database user through `node command '<cmd>'
  --on n1` (no `--sql`): the health check's workspace
  `/opt/pgedge/regression/ace` already holds `ace.yaml` and the service
  file; `cd` there first and export `ACE_PGSERVICEFILE` as
  `aspects/health_check/ace.py` does (read `service_file()` and
  `environment` there). Then `ace table-diff <cluster> <schema.table>
  [--block-size N] [--nodes n1,n2] [--table-filter '<predicate>']
  [--output json]`, `ace table-repair <cluster> <schema.table>
  --diff-file <path> --source-of-truth <node> [--dry-run] [--upsert-only]`,
  `ace table-rerun <cluster> --diff-file <path>`, `ace schema-diff`,
  `ace repset-diff`, `ace spock-diff`. `ace --help` and each
  sub-command's `--help` are the authority on flag names; quote the
  version from `ace --version`.
- `./pg_cluster_ctl.sh node command '<SQL>' --sql --on n2` to plant
  divergence: `SELECT spock.repair_mode(true); UPDATE ...;` in one
  `-c` (same session), `spock sub-show-status`, `app install --app
  pgbench --scale 10` for a large table, `app counts`.
- `./pg_deploy_cluster.sh --health_check ace` once at the start and once
  at the end.

Practical: everything in a `deep_ace` schema created through
`spock.replicate_ddl` and added to the default repset. After every planted
divergence, a `diff table` before the repair and one after, both quoted.
Repairs replace or merge the target's copy of the table; `--no-truncate`
merges. Long diffs on big tables go in the background with a generous
timeout; serialize SSH commands against one host.

## 2. Scenarios

Record for each: the commands, the key output, PASS / FAIL / NOT RUN with
the reason, and timings. Stop at the first scenario whose cleanup you
cannot complete.

1. **Baseline.** `--health_check ace` passing (or the binary reported
   missing, in which case the ACE half of every scenario is NOT RUN with
   that reason); `diff all` clean and exit 0; `ace --version`;
   subscriptions replicating.
2. **A clean table is reported clean, by both.** A `deep_ace.rows` table
   of ten thousand rows with a bigint key, text, numeric, timestamptz,
   jsonb, bytea, a text[] column and NULLs in every column, replicated.
   `diff table` and `ace table-diff` both report no difference. Time
   both.
3. **Every kind of row divergence, one at a time.** On n2 in repair mode:
   change one text value; change a numeric by one cent; set a column to
   NULL; delete one row; insert one extra row; change a jsonb key order
   only (semantically equal); change a bytea; change a timestamptz by one
   microsecond. After each, `diff table` (with drill-down) and `ace
   table-diff` must name the row and the node. Record which of the two
   misses what. Repair with `diff repair --source n1` and confirm clean.
4. **Scoping.** With three planted differences, `--where` / `--table-
   filter` narrowing to one of them finds exactly one; `--buckets` and
   `--block-size` small enough to split the table report the same rows
   as the defaults; `--no-drill-down` reports buckets, not keys.
5. **Repair semantics.** Plant an extra row on n2 and a missing row on n3.
   `diff repair --source n1 --targets n2` fixes n2 only; with
   `--no-truncate`, a target's extra row survives (document it as
   expected) while missing rows are filled; without it the target's copy
   is replaced. `ace table-repair --dry-run` changes nothing and says
   what it would do; a real `table-repair` from a diff file fixes it;
   `table-rerun` on the saved diff file then reports clean. A repair on a
   table with no difference is a no-op and says so.
6. **Repair must not replicate.** After a repair, the source node's rows
   must not have been rewritten on every peer through Spock (check
   `spock.resolutions` count and lag before and after); the repair goes
   to the targets directly.
7. **Tables ACE cannot compare.** A table without a primary key: both
   implementations must refuse, with a clear message, not report clean.
   A table in no replication set: both must still compare it (data is
   data). A table with a composite primary key: differences detected and
   repaired correctly.
8. **Schema drift.** `ALTER TABLE deep_ace.rows ADD COLUMN extra int` on
   n3 without `replicate_ddl`: `diff schema` and `ace schema-diff` name
   the column and the node; an index on one node only; a NOT NULL on one
   node only. Repair by hand through `replicate_ddl` and confirm clean.
9. **Replication-set and Spock metadata drift.** A table added to the
   default repset on one node only: `diff repset` / `ace repset-diff`
   name it. A subscription disabled on one node: `diff spock` /
   `ace spock-diff` notice (or do not: report either). The interfaces
   `<node>_promoted_<port>` a past failover leaves on peers: say whether
   spock-diff flags them, and file the answer as expected behaviour or a
   finding with reasons.
10. **A big table.** pgbench at scale 10 (one million accounts) replicated;
    plant one hundred scattered differences on n2 in one repair-mode
    transaction; time `diff table pgbench_accounts` with default and with
    larger buckets, and `ace table-diff` with default and larger
    `--block-size`; all one hundred found, none extra; repair; clean.
    Memory or time blow-ups are findings.
11. **A node that is down.** Stop Patroni on a Spock node without a
    standby; `diff all` and `ace table-diff` must fail clearly for that
    node and still compare the others (or say they cannot); start it
    again, under two minutes down.
12. **Logs.** ACE's own JSON reports in its workspace output directory,
    and `service logs --component postgres` on every node: every ERROR,
    FATAL, PANIC, each marked expected or not.
13. **Restore and verify.** `deep_ace` dropped through `replicate_ddl` on
    every node, pgbench removed, no planted difference left (`diff all`
    clean, exit 0), ACE's reports left where the health check keeps them
    only, subscriptions replicating, `--health_check ace` passing.
    Anything you could not restore goes at the top of the report.

## 3. The report

Write it to the scratchpad as `ace-deep-test-report.md`, and summarize it
in the final message. Node names only: never a host address, hostname,
username, key path or password; ACE prints service names and this tool
prints the address, so redact. Per scenario: what was done, what happened,
the verdict, the timings in a table (both implementations side by side).
End with a ranked list of findings, each labelled as one of **bug in
ACE** (version, exact command and output), **bug in
postgres-cluster-deployment** (`aspects/consistency.py` or the health
check, with the behavior), or **topology limitation**. Where the two
implementations disagree, say which one is right and how you know.

Report faithfully. A step that hung or failed is reported with its output,
never guessed at.
