---
name: spock-pr-on-cluster
description: |
  Validate a pgEdge/spock pull request, commit or branch against the cluster
  this repository has already deployed, instead of a throwaway two-node
  sandbox. Use when asked to test, validate, verify or run a Spock PR (or a
  commit/branch of Spock) on the running cluster, the deployed topology, or
  "my cluster". Builds the change onto every node with `spock rebuild`, runs
  the suite and PR-specific scenarios before and after, uses the topology's
  own HA (Patroni failover, standbys, 3+ peers) as test surface, restores the
  original build, and writes a report. Do not use for a PR in another
  repository, or when no cluster is deployed (deploy one first, with a
  topology whose Spock major matches the PR's base branch).
---

# Validate a Spock PR on the deployed cluster

Input: a pgEdge/spock PR number or URL, a commit, or a branch. Optional: a
cluster name (default: the most recently deployed), a focus area.

Goal: show what the change does on a real topology, which is what the
sandbox skill cannot show: under Patroni failover, across three or more
peers, next to lolor and snowflake, on packaged PostgreSQL. Prove the bug
exists before, is gone after, and nothing adjacent broke. Report in plain
bullets, verdict first.

Everything below runs through this repository's own scripts. They are the
only supported way to touch the cluster: `pg_cluster_ctl.sh` goes through the
saved state and the executor, so the same commands work on localhost, on one
VM, or on twenty. Never `pg_ctl`, never `systemctl restart`, never edit a
config by hand. Patroni owns PostgreSQL; going around it looks like a crash.

## 1. Understand the change

- PR: `gh api repos/pgEdge/spock/pulls/<N>` then `.../pulls/<N>/files`. If
  `gh` is not logged in, `curl -s https://api.github.com/repos/pgEdge/spock/pulls/<N>`
  works unauthenticated for a public repo.
- Commit or branch: `git clone --filter=blob:none https://github.com/pgEdge/spock.git`
  into the scratchpad, then `git show`, `git branch -r --contains`.
- Write down: the claimed bug, its trigger, files touched, tests added, and
  which side it touches (provider hook, output plugin, apply worker, SQL
  functions, catalog/views). This decides the scenarios in step 5.
- Note the PR's **base branch**: `main` is Spock 6, `v5_STABLE` is Spock 5.
  This matters in step 2.

## 2. Discover the cluster and check it fits

```bash
./pg_cluster_ctl.sh node list
./pg_cluster_ctl.sh service status
./pg_cluster_ctl.sh spock version
./pg_cluster_ctl.sh spock sub-show-status
```

From `spock version`: `loaded` is the library build, `extension` the SQL
version created, `available` what the control file offers. All nodes must
agree, and `loaded` must match the PR's base major, or stop:

- **Major mismatch** (cluster on 5.0.x, PR against `main` = 6.0.0): do not
  proceed. `make install` would put a Spock 6 library under a Spock 5
  catalog. Say so and deploy a matching cluster first:
  `./pg_deploy_cluster.sh --topology 3-node-extensions-source` builds Spock
  from `main`; `--spock-major 60` on any topology does the same for packages.
- **Nodes disagree**: fix that first (`spock rebuild` at the current ref, or
  `package upgrade`), or the before/after comparison is meaningless.
- **Not healthy** (`sub-show-status` not all `replicating`, a node `DOWN`):
  fix that first. A validation on a broken cluster reports the breakage, not
  the PR.

Record the output: it is the "before" column of the report, and the ref to
restore to in step 7 is `spock rebuild`'s `previous_ref` or the package.

Also write down which nodes have a standby (`FOLLOWS` column of `node list`).
Only those can fail over, and that is a scenario the sandbox cannot run.

## 3. Baseline on the current build

Run everything you will run again after the rebuild, so the comparison is
like for like. Keep every output under `validation/<pr-or-sha>/before/`.

```bash
./pg_deploy_cluster.sh --health_check spock                 # the in-tree suite
./pg_cluster_ctl.sh diff all                                # peers agree?
./pg_cluster_ctl.sh spock lag
./pg_cluster_ctl.sh node command "SELECT ..." --sql --compare   # PR-specific
```

`node command --sql --compare` runs one statement on every node and flags
any that answers differently; it is the fastest way to ask a question of the
whole mesh. Scenario SQL files are run on one node with
`./pg_cluster_ctl.sh node command "$(cat file.sql)" --sql --on n1`.

If the PR adds regression tests under `tests/regress`, read them for the
*behaviour* they check, then rewrite that check as a scenario here. They
cannot run as-is: upstream's `init.sql` creates nodes, roles and `trust`
auth that a deployed cluster must not have. `aspects/health_check/spock.py` explains
this and `aspects/health_check/suites/spock/sql/` shows the pattern: nothing created
except a `spock_regress` schema, peers found through `spock.node_interface`,
checks that print fixed `name|true` lines.

Expect the pre-fix run to *fail* the PR's scenario. If it passes before the
change, either the scenario does not reach the bug or the cluster already
has the fix; say which.

## 4. Put the change on the cluster

```bash
./pg_cluster_ctl.sh spock rebuild --ref pull/<N>/head --yes
./pg_cluster_ctl.sh spock version
```

`--ref` takes a branch, a tag, a commit, or `pull/<N>/head`. The command
builds on every host against that host's own `pg_config`, restarts
PostgreSQL on every node one at a time through patronictl, runs
`ALTER EXTENSION spock UPDATE` where the SQL version moved, and prints
loaded/created versions before and after, per node. A build failure on any
host restarts nothing.

Check `spock version` afterwards: every `loaded` must show the new build and
every `checksum` must agree. If `extension` lags `available`, the update
failed and the output says why (usually an upgrade script the PR did not
ship; that is a finding).

The cluster is live throughout. If something is actively writing to it,
the rolling restart will briefly fail over any scope that has a standby;
that is fine, and the auto-repair callback (if on) or
`pg_cluster_status.sh --repair-failover` handles the Spock side.

## 5. Scenarios

Re-run step 3 into `validation/<pr-or-sha>/after/`, then the PR-specific
scenarios. Choose them from what the change touches, and use what the
topology gives you:

- **Multi-peer**: a 3+ node mesh forwards changes; check the row arrives on
  *every* peer, not just the one subscribed to the origin. `diff all` after
  each scenario.
- **Failover during the operation**: with the scenario's workload running,
  `service switchover --node n1` (or `service stop --node n1` for an
  unplanned one), then `--repair-failover n1` if auto-repair is off. Does
  the change survive the apply worker reconnecting to a promoted standby?
  Does it survive being on the standby that was promoted?
- **Apply side, not just origin**: run the trigger on n1, verify on n2 and
  n3. Many upstream tests only check the origin.
- **Restart the apply worker**: `spock sub-disable` then `sub-enable`
  on a peer; look at `spock.sub_show_status` and, for monitoring PRs, at
  whatever event/history view the PR adds.
- **The other extensions**: if lolor or snowflake are present, repeat the
  core scenario on a table using them.
- **Loop**: 20 iterations of the trigger, then check logs on every node:
  `./pg_cluster_ctl.sh service logs --node <n> --component postgres --lines 300`
  and grep `ERROR|TRAP|PANIC|terminated by signal|FATAL`.
- **Session state**: after the command, `pg_stat_activity.state`,
  `txid_current_if_assigned()`, no leaked transaction.
- **Non-superuser caller** where the change has a SQL surface.

Every scenario is run on the same nodes before and after, and its outputs
are kept. A scenario that creates objects must drop them; the cluster is
left as it was found.

## 6. Decide

For each scenario: before result, after result, on which node(s). A PR
passes when its own scenario fails before and passes after, every other
scenario is unchanged, `diff all` is clean, logs show nothing new, and
`spock version` agrees across nodes. Anything else is a finding with a
minimal repro.

## 7. Restore

Unless asked to leave the PR build in place:

- Source-built cluster: `./pg_cluster_ctl.sh spock rebuild --ref <previous_ref> --yes`
  (`spock rebuild` printed it; it is also in the cluster state as
  `metadata.spock_rebuild.previous_ref`).
- Packaged cluster: `./pg_cluster_ctl.sh node command 'dnf reinstall -y pgedge-spock<major>_<pg>' --on all`
  (`apt-get install --reinstall pgedge-postgresql-<pg>-spock<major>` on
  Debian), then `./pg_cluster_ctl.sh service restart --node <n> --component postgres`
  for each node, then `spock version`.

An extension whose SQL was upgraded (5.0.x to 6.0.0) does not downgrade. If
the PR moved the extension version, restoring the library alone leaves a
catalog the old library cannot serve; say so, and either leave the PR build
in place or redeploy the cluster.

Verify with `spock version`, `service status` and `spock sub-show-status`
that the cluster is back where step 2 found it.

## 8. Report

Save to `validation/<pr-or-sha>-<slug>.md` in this repository. No customer
names, hostnames, IPs or credentials; node names (`n1`, `n1s1`) are fine.

- Verdict first: PASS / FAIL / INCONCLUSIVE, one line why.
- Cluster: topology, PostgreSQL version, Spock loaded before and after,
  which nodes had standbys.
- Table: scenario | before | after | nodes.
- New bugs found: cause, impact, minimal repro, log excerpt.
- Coverage gaps: what the PR's own tests do not reach that the cluster
  showed.
- What was restored, and anything left changed.

Offer a GitHub issue or JIRA (SPOC-) draft for any finding.

## Pitfalls

- `spock rebuild` replaces the module for **every** node on a host that
  shares a PostgreSQL prefix, which is why it restarts every node. Do not
  run it with `--no-restart` and then restart only some nodes.
- Patroni restarts go through `patronictl`; `service restart --component postgres`
  is the only restart to use. A `pg_ctl restart` looks like a crash and
  Patroni fails the scope over.
- A deployed cluster uses scram auth and real passwords. Do not create
  roles, do not edit pg_hba, do not lower anything to make upstream tests
  run. Rewrite the test instead.
- The regression workspace is `/opt/pgedge/regression` on the host;
  artefacts of a failed suite are there, and `--health_check` prints the path.
- Logs of every `spock rebuild` and `--health_check` run are under `logs/`
  in this repository, named by run id; the command prints the directory.
