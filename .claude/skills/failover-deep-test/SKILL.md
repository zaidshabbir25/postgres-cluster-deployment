---
name: failover-deep-test
description: |
  Test Patroni failover of the cluster this repository has deployed, in
  depth and from the laptop: slot copies on the standby (including the idle
  primary case), the built-in failover suite, failover under load, the
  promoted standby serving the mesh, the old leader rejoining, the
  switchover guard, two failovers in a row, a postmaster crash, loss of the
  standby under synchronous replication, and a full restore. Use when asked
  to test, exercise, stress or find bugs in failover, switchover, standbys,
  slot copies or HA on the running cluster or "my cluster". Disruptive: it
  stops Patroni, kills a postmaster and promotes the standby. Do not use on
  a cluster that has no Spock node with a standby (add one first with
  --add-node --role standby --leader <node>), or on a production cluster.
---

# Failover, in depth, on the deployed cluster

Input: optional cluster name (default: the most recently deployed), optional
list of scenarios to skip or focus on.

Goal: find out what really happens to this topology when a scope's leader
goes away, in more ways than `--health_check failover` tries, and sort every
oddity into one of three bins: a bug in Spock, a bug in this tool, or a
limitation of the topology. The cluster is the harness, so nothing may be
left behind, and the scope must end where it started.

Everything runs through this repository's scripts. Never `pg_ctl`, never
`systemctl`, never `spock rebuild`, never an edit to a config file by hand:
Patroni owns PostgreSQL and going around it looks like a crash. Do not edit
any file in the repository while testing; findings go in the report.

## 0. Read the plan before touching anything

`configuration/clusters/<name>.json` holds the plan. Write down, in node
names only:

- the Spock nodes, and which of them have standbys (`standbys`). The subject
  is a Spock node with a standby; its scope is `pgedge-<node>`.
- the peers: every other Spock node. Each peer holds `sub_<subject>_<peer>`
  pointed at the subject, and the subject holds `sub_<peer>_<subject>`.
- `synchronous_mode`, `synchronous_node_count`, `synchronous_mode_strict`.
  With strict on, the subject refuses commits while its standby is down.
  That is a feature, and it shapes scenario 9.
- `auto_repair_failover`. Off means the peers are retargeted by hand with
  `./pg_cluster_status.sh --repair-failover <subject>`; on means the Patroni
  callback does it and you only wait and verify.
- whether the standby shares a host with its leader. Then a failover is a
  port change, not a host change, and "the host died" cannot be simulated.
- the Spock major and PostgreSQL major. Spock 6 on PostgreSQL 17 and later
  copies slots only through `sync_replication_slots`; the suite says so in
  its verdict when that is off.

Model to keep in mind: the standby is a byte copy of the subject. Promoted,
it serves the same Spock node identity on its own port. The peers must then
be moved to an interface named `<subject>_promoted_<port>`. Spock copies the
subject's provider slots (`spk_<db>_<subject>_sub_<subject>_<peer>`) to the
standby; a copy that is missing or temporary at promotion time means the
promoted node has no slot and outbound replication to that peer is gone.
That is the single most important thing to verify in every scenario.

## 1. The tools

Activate the virtualenv first (`source venv/bin/activate`).

- `./pg_cluster_status.sh [--json]`: health, slots, lag; exits non-zero when
  anything is wrong. `--repair-failover <subject>` asks each peer where its
  subscription points and retargets the ones that do not point at the
  scope's current leader.
- `./pg_cluster_ctl.sh node command '<SQL>' --sql --on n1,n1s1,n2` (also
  `--compare`, `--on all`; without `--sql` it is a shell command). Standbys
  are nodes too.
- `./pg_cluster_ctl.sh spock sub-show-status | lag | version | sub-enable |
  sub-disable | replicate-ddl`
- `./pg_cluster_ctl.sh db guc-show --pattern 'spock%'`, `db guc-set`,
  `db guc-reset`.
- `./pg_cluster_ctl.sh service status | stop | start | restart --node X
  --component patroni|postgres | logs --node X --component patroni|postgres
  --lines N | switchover --node <subject> --candidate <member> [--force]`.
  The switchover refuses a candidate without permanent `spk_*` copies of
  every slot unless `--force`.
- `./pg_cluster_ctl.sh app install --app pgbench --scale 5`, `app counts`,
  `app remove`; `./pg_cluster_ctl.sh diff all`.
- `./pg_deploy_cluster.sh --health_check failover`: the built-in four
  cases. Its logs are under `logs/<run-id>-pgedge-health-check/`:
  `deploy.log` has every command and output, `failover-regression.log` the
  verdicts.
- SQL worth repeating: `SELECT slot_name, active, temporary, restart_lsn,
  confirmed_flush_lsn FROM pg_replication_slots WHERE slot_name LIKE 'spk\_%'`;
  `SELECT pg_is_in_recovery()`; `SELECT * FROM spock.sub_show_status()`;
  `SELECT sub_name, sub_enabled, sub_origin_if FROM spock.subscription`;
  `SELECT * FROM spock.node_interface`; `SELECT * FROM pg_stat_replication`.
  Shell on the subject's host: `patronictl -c /etc/patroni/<node>.yml list`
  and `show-config`.

Practical: `spock.sub_disable(name, true)` cannot run inside a transaction
block, so one statement per `node command`. Anything that may take more
than a minute runs in the background with a generous timeout, and SSH
commands against one host are serialized, not fanned out. A write that
hangs on the subject while its standby is down is strict synchronous mode
at work, not a bug.

## 2. Scenarios

Record for each: the commands, the key output, PASS / FAIL / NOT RUN with
the reason, and elapsed time where a wait was involved. Stop at the first
scenario whose cleanup you cannot complete; a half-restored cluster is worse
than a short report.

1. **Baseline.** status, service status, `patronictl list`, sub-show-status,
   lag, `spk_*` slots on subject, standby and peers, spock version,
   `show-config` (synchronous settings, permanent slots, callbacks), diff
   all. Everything healthy, or stop and report.
2. **Slot copies on the standby.** Count equals peers, `temporary` false.
   Create a test schema and table through `spock.replicate_ddl`, add it to
   the default repset, write to it from the subject, and confirm the copy's
   `confirmed_flush_lsn` on the standby advances. Then the idle primary case
   (upstream issue pgEdge/spock#730): no writes for about ninety seconds,
   polling every fifteen, and the copy must stay permanent throughout.
3. **The built-in suite.** `--health_check failover` once; record the four
   verdicts and timings from its log. A second run must pass too (interfaces
   from the first run are reused, not recreated).
4. **Failover under load.** pgbench writing on the subject and on a peer at
   the same time; stop Patroni on the subject in the middle. Time the
   promotion. Repair, re-enable, verify replicating both ways, verify the
   promoted node carries its `spk_*` slots, confirm rows written on the
   peer during the outage arrived and vice versa, diff all. Say whether the
   subject's pgbench aborted (expected) and whether the peer lost anything
   (not expected).
5. **The promoted standby serves the mesh.** Writes from it reach the peers,
   writes from the peers reach it, `replicate_ddl` from it reaches the
   peers. Start Patroni on the old leader: it must rejoin as a streaming
   replica (note whether pg_rewind ran, from its Patroni log), and `spk_*`
   copies must appear on it, polled for up to three minutes.
6. **The switchover guard.** Try the switch back before the copies have
   arrived if you can catch that window: expect a refusal naming "0 of N
   spk_* slot copies". After they arrive, switch back and expect success.
   Then `--repair-failover` must say the peers still point at the standby's
   port and retarget them; sub-show-status replicating, diff all clean.
7. **Two failovers in a row.** Repeat a quick stop-Patroni failover and
   repair. No "add interface failed" line may appear; the peer reuses the
   `<subject>_promoted_<port>` interfaces. Switch back as in 6.
8. **Crash, not a clean stop.** `kill -9` the subject's postmaster (its pid
   is the first line of `postmaster.pid` in the data directory). Does
   Patroni restart it in place or fail over, how long, and does replication
   to the peers resume without a retarget? Restore the baseline afterwards.
9. **Standby loss.** Stop Patroni on the standby only. With strict
   synchronous mode, writes on the subject block while reads work and
   peer-to-subject replication stalls; without it, nothing blocks. Start
   the standby again, the blocked write completes, the slot copy returns.
   Keep the standby down under two minutes.
10. **Logs.** `service logs --component postgres` and `--component patroni`
    on the standby and the subject around each promotion. List every
    ERROR, FATAL and PANIC and say whether it is expected.
11. **Restore and verify.** Subject leads, standby streams, every
    subscription replicating, lag near zero, diff all clean, `spk_*` copies
    on the standby, test schema and pgbench removed on every node, the
    status script exits zero. Anything you could not restore goes at the
    top of the report.

## 3. The report

Write it to the scratchpad as `failover-deep-test-report.md`, and summarize
it in the final message. Node names only: never a host address, hostname,
username, key path or password, and the status output prints the address,
so redact it. Per scenario: what was done, what happened, the verdict, the
timings. End with a ranked list of findings, each labelled as one of:

- **bug in Spock**, with the version and the exact observation;
- **bug in postgres-cluster-deployment**, with the file and the behavior;
- **topology limitation** (single host, strict synchronous mode, no DCS
  quorum), which is worth knowing but not fixing here.

Report faithfully. A step that hung or failed is reported with its output,
never guessed at.
