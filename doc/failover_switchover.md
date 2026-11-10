# Failover and switchover

How to move a Spock node's leadership from its machine to its standby, on
purpose or in a test, and how to get the old machine back afterwards.

## The one thing to understand first

The cluster has two replication layers doing different jobs, and a failover
only touches one of them.

- **Spock** is multi-master. `n1`, `n2`, `n3` hold *different* data and
  replicate to each other in both directions.
- **Patroni** is HA inside one Spock node. `n1` and `n1s1` hold the *same*
  data. `n1s1` is a physical copy of `n1` and nothing else.

So a failover changes **which machine serves `n1`**. It never changes which
data `n1` holds, and it never involves `n2` or `n3`.

The consequence that matters in practice: Patroni moves the leader, but the
Spock peers were given `n1`'s address when the mesh was wired and they keep
using it. They do not notice the move. Retargeting them is a separate step,
and it is not optional — see [After any failover](#after-any-failover-retarget-the-peers).

## Before you start: which nodes can fail over

Only a node that has a standby. Check:

```bash
./pg_cluster_ctl.sh service status
```

```
NODE  HOST   PATRONI  ENABLED  PG_ROLE  PG_STATE  ETCD
n1    local  active   enabled  primary  running   active
n2    local  active   enabled  primary  running   active
n1s1  local  active   enabled  replica  running   active
```

Here `n1` can fail over to `n1s1`. `n2` has no standby, so stopping it does not
fail anything over — its scope simply has no leader until you start it again.
The other Spock nodes keep accepting writes throughout, because they hold
different data.

To give a node a standby:

```bash
./pg_cluster_ctl.sh node add --role standby --leader n2
```

The standby is placed on a host other than its leader's where one is free,
since a standby sharing its leader's machine protects against nothing but a
process dying.

## Moving a leader on purpose: switchover

This is the right command for maintenance, a host you need to reboot, or
rebalancing. It hands over through the DCS, so there is no window without a
leader.

```bash
./pg_cluster_ctl.sh service switchover --node n1
./pg_cluster_status.sh --repair-failover n1
```

`--candidate` picks a specific standby if the node has more than one; without
it the first one is used. The command refuses up front if the node has no
standby, or if the candidate is too far behind to be promoted safely.

## Testing an unplanned failure: stop Patroni

Stopping a node's Patroni is how you rehearse a crash. Patroni shuts
PostgreSQL down, releases the leader key, and the standby is promoted within a
few seconds.

```bash
./pg_cluster_ctl.sh service stop --node n1
./pg_cluster_status.sh --repair-failover n1
```

Two things to know before you do this:

**Do not stop PostgreSQL directly.** Patroni owns the postmaster. If you stop
it behind Patroni's back it looks like a crash, and Patroni restarts it
immediately or fails over anyway. `service stop --component postgres` refuses
for this reason. Stop Patroni instead, which stops PostgreSQL with it.

**A lagging standby will not be promoted.** Patroni refuses to promote a
standby more than 1 MB behind (`maximum_lag_on_failover`). Under write load a
stop can leave the scope with no leader rather than failing over. That is the
setting protecting you from losing those writes, not a fault.

## After any failover: retarget the peers

```bash
./pg_cluster_status.sh --repair-failover n1
```

Run this **immediately after the switchover or stop**, not later, and not only
when the old machine comes back.

Until you do, the peers are still replicating from an address that is now a
read-only replica (or nothing at all). Replication into that scope is stalled,
and — the part that actually hurts — the Spock provider slots on the promoted
node retain WAL the whole time. Leave it for hours and you are filling that
host's disk.

The command is safe to run when nothing has moved: it checks who leads the
scope and says there is nothing to do.

To have this happen on its own, see
[Doing the retarget automatically](#doing-the-retarget-automatically).

`pg_cluster_status.sh` also flags this on its own, so a node reported as
`replica` where you expect a leader means the retarget has not been done.

## Doing the retarget automatically

The repair above can be handed to Patroni instead of being remembered:

```bash
./pg_cluster_ctl.sh service auto-repair --on
./pg_cluster_ctl.sh service auto-repair          # show the current setting
./pg_cluster_ctl.sh service auto-repair --off
```

It is off by default, and can also be switched on at deploy time with
`--auto-repair-failover`.

Turning it on writes a small shell script onto every member, names it as
Patroni's `on_role_change` callback in each local `patroni.yml`, and reloads
Patroni. Nothing restarts, no leader moves, and the cluster stays up
throughout.

After that, a promoted standby retargets the peers itself, within a second or
so of taking the leader lock. `--repair-failover` still works and is still
worth running if you want to be sure.

**How it works, in case you have to debug it.** `on_role_change` fires on the
member whose role changed, with the new role as an argument, so the script
runs on the node that was just promoted. It exits immediately unless the role
is `primary`, which means the demoted old leader does nothing. It then asks
its own `spock.sub_show_status()` for the peers — that list is current and
needs no maintenance, so a cluster that gains a node later needs no new
scripts — and runs the same four statements on each peer that
`--repair-failover` does.

Each node logs every run:

```
/var/log/pgedge/<node>-role-change.log
```

The script is deliberately self-contained — shell, no Python, no repository
checkout, no SSH. The machine that ran the deployment may be a laptop that is
closed when the failover happens; a cluster host can always reach its peers.

**What to expect from it.** Patroni ignores the script's exit status, so the
log is the only record — check it first. One unreachable peer does not stop
the others being repaired. A scope that flaps fires the callback repeatedly,
which is safe: the repair is idempotent, and an interface that already exists
is treated as success rather than an error.

It does not change what a failover costs, only who notices. The subscriptions
are still briefly disabled and re-enabled on each peer.

## Bringing the old node back

Start it the normal way, whether that is five minutes or five hours later:

```bash
./pg_cluster_ctl.sh service start  --node n1
./pg_cluster_ctl.sh service status --node n1
```

It rejoins as a **replica of `n1s1`**. Patroni never fails back on its own, so
`n1s1` keeps the lead until you move it deliberately.

There is nothing to do on the Spock side. `n1s1` holds the same data and has
been serving as Spock node `n1` since it was promoted; the returning machine is
a physical replica again, with no Spock identity of its own. **Do not run
`node add`** for it — that would try to join it as a new multi-master peer.

### What Patroni does to catch it up

One of three things, picked automatically:

1. **Follows the new timeline from WAL.** Fast, nothing to do.
2. **Runs `pg_rewind`,** if the WAL it needs is gone. This works here because
   the cluster is initialised with `data-checksums`, which `pg_rewind`
   requires.
3. **Re-clones the whole data directory** from the leader, if the rewind
   fails. Correct, but it copies the entire database.

**Expect (2) or (3) after a long outage.** Patroni keeps a replication slot for
each member it can see. Once a node's Patroni is stopped, its key in etcd
expires in 30 seconds, the leader stops counting it as a member, and the slot
protecting its WAL is dropped. The permanent slot this tool configures is named
after the *standby*, which protects the standby while it is down — the normal
direction. Nothing protects the old leader once the roles have swapped. After a
few hours of writes, assume its WAL is gone.

To force the clean path rather than waiting to find out:

```bash
./pg_cluster_ctl.sh service reinit --node n1s1 n1
```

That rebuilds `n1` from the current leader, discarding its data directory. It
asks for confirmation unless you pass `--yes`.

## Failing back

Once the returned node is caught up — `service status --node n1` shows it as
`replica` / `running` — hand leadership back:

```bash
./pg_cluster_ctl.sh service switchover --node n1 --candidate n1
./pg_cluster_status.sh --repair-failover n1
```

Switching to a member that is still catching up is refused. That refusal is
correct; wait and try again.

## Command reference

| Task | Command |
| --- | --- |
| See every node's services | `./pg_cluster_ctl.sh service status` |
| See one node | `./pg_cluster_ctl.sh service status --node n1` |
| Planned leader move | `./pg_cluster_ctl.sh service switchover --node n1` |
| Simulate a failure | `./pg_cluster_ctl.sh service stop --node n1` |
| Bring a node back | `./pg_cluster_ctl.sh service start --node n1` |
| Retarget peers after a move | `./pg_cluster_status.sh --repair-failover n1` |
| Do that automatically from now on | `./pg_cluster_ctl.sh service auto-repair --on` |
| Rebuild a member from its leader | `./pg_cluster_ctl.sh service reinit --node n1s1 n1` |
| Restart PostgreSQL only | `./pg_cluster_ctl.sh service restart --node n1 --component postgres` |
| Reload config, no restart | `./pg_cluster_ctl.sh service reload --node n1` |
| Recent logs | `./pg_cluster_ctl.sh service logs --node n1 --lines 100` |

`--component` takes `patroni`, `postgres` or `etcd`, and defaults to `patroni`.
`service restart --component postgres` goes through `patronictl restart`, so
the rest of the scope knows the restart was deliberate — that is the one to use
after changing a setting that needs a restart.

## When it does not work

**"scope … has no standby to switch over to"** — that node has no replica. Add
one with `node add --role standby --leader n1`, or expect a stop to leave the
scope leaderless.

**The stop did not promote anything.** Either the node has no standby, or the
standby was lagging past `maximum_lag_on_failover`. Check
`./pg_cluster_ctl.sh service logs --node n1s1`.

**Writes to `n1` fail after a failover.** The peers have not been retargeted.
Run `./pg_cluster_status.sh --repair-failover n1`.

**Disk filling on the promoted host.** Same cause: inactive Spock slots holding
WAL for peers that cannot reach the scope. The retarget releases them.

**A restarted node stays in `starting`.** It is rewinding or re-cloning. Watch
`service logs --node n1`; for a large database, (3) takes as long as a full
copy.

## Testing this on a single machine

Every node on one host exercises Patroni's logic but not host failure — they
share a kernel, a disk and an etcd. It is a good way to learn the commands and
a poor way to measure whether you would survive losing a machine.
