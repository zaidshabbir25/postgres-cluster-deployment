# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
source venv/bin/activate                    # the shell entry points create this on first run
pip install -r requirements.txt -r requirements-dev.txt

pytest                                      # whole suite, ~5s, touches no machine
pytest tests/test_topology.py               # one module
pytest tests/test_topology.py::test_name    # one test
pytest -k "version"                         # by expression

bash -n pg_deploy_cluster.sh                # shell syntax, what CI checks
python -m pyflakes aspects deployment | grep "undefined name"
```

`tests/test_static_checks.py` runs that last one for you and fails only on
*undefined names* — a bare pyflakes run exits non-zero on ~20 pre-existing
unused imports, which are deliberately not treated as errors.

`pytest.ini` sets `pythonpath = . tests`, so test modules import the fakes as
`from conftest import FakeExecutor` **or** `from tests.conftest import ...` —
both work, and both spellings exist in the suite.

CI (`.github/workflows/regression.yml`) runs pytest on Python 3.9 and 3.12,
`bash -n` on every `*.sh`, and a size/landmark check on the entry scripts (a
commit once truncated `pg_deploy_cluster.sh` to one byte). shellcheck runs
advisory-only.

## Architecture

### Two replication layers, doing different jobs

This is the mental model everything else depends on.

- **Spock** is multi-master: every Spock node accepts writes and replicates
  logically to every peer, both directions, cross-wired into a full mesh.
- **Patroni** is HA *within* one Spock node: each Spock node leads a scope of
  its own, and its standbys are physical replicas of that node alone. A
  failover changes which machine serves `n1`, never which data `n1` holds.

So `n1` and `n2` hold different data; `n1` and `n1s1` hold the same data. Code
that treats "node" as one concept is usually wrong — check `node.is_spock`.

### `aspects/` knows how, `deployment/` decides what

Nothing in `aspects/` imports from `deployment/`. Every `aspects` function
takes an **executor** as its first argument (`ssh_executor.build_executor`
returns an `SSHExecutor` or a `LocalExecutor` with the same interface), which
is why the same code drives one VM or twenty — and why the whole test suite can
run without a machine.

`aspects/cluster_model.py` (`Host` / `Node` / `ClusterPlan`) is a leaf: anything
it needs from the rest of `aspects` is imported *inside the function*, not at
module scope. Preserve that if you extend it.

`aspects/health_check/` holds what `--health_check` runs, one module per
component. Each subclasses `health_check.runner.Suite` and answers three
questions in order — can this run here, put what it needs on the host, run it
how — and each works in its own directory under `/opt/pgedge/regression`.
spock and lolor are `InTreeSuite`s: their cases live in `suites/<name>/sql`
beside fixed `expected/` files, run through psql and dblink against the live
mesh, and create nothing but a schema of their own. `spock-upstream` and
`lolor-upstream` are `PgRegressSuite`s: upstream's own cases, cloned fresh at
the cluster's ref on every run and executed as the database user in a scratch
instance with the installed library — only ever `regresscheck` for spock,
since upstream's `check` would `make install` over the running `spock.so`. A
component name expands to both halves (`GROUPS`). The distinction that matters
is `targets_cluster` plus `scope`: spock, lolor, snowflake and ace (the
installed binary's spock/schema/repset-diff) exercise the deployed cluster;
the `-upstream` suites exercise the installed library; `ace-integration`
starts its own containers, proves nothing about the cluster, and is left out
of `all`. `failover` (`health_check/failover.py`) is the one suite that
changes the cluster: it stops Patroni on the leader of a scope with a standby,
checks the promotion (the `spk_*` slot copies, peers retargeted by the
callback or by hand, rows kept and replicating both ways, the old leader
rejoining), then switches the scope back. By name only, like
`ace-integration`, but because it is disruptive rather than irrelevant. Its
waits go through `_now`/`_sleep` so tests run a simulated clock.

`deployment/cli.py` holds lifecycle commands (deploy, plan, status, remove) and
`deployment/ops_cli.py` registers the day-two groups (node, spock, db, service,
package, app, diff) into the same argparse tree. The `.sh` entry points are thin
wrappers over `python3 -m deployment.cli`.

### State, logs and reports

`aspects/state.py` persists one JSON per cluster under
`configuration/clusters/`. Every day-two operation loads it rather than
re-deriving a plan. Adding a `ClusterPlan` field means old state files still
load (dataclass defaults) but new ones break older checkouts.

`aspects/logging_setup.RunLogger` owns the step timeline: `step_start` /
`step_end` write `logs/<run-id>/steps.json`, and results flow into
`reports/`. **Step names are load-bearing** — `aspects/setup_report.py` maps
components to the steps responsible for them by name (`STEPS_FOR`), so renaming
a step silently turns its rows into "not reached".

## Invariants worth knowing before changing things

**zodan ordering.** `spock.add_node` (from the Spock repo's `samples/Z0DAN/`)
verifies the *joining* node's database is clean — it refuses one carrying the
lolor schema, or any user table at all. So optional extensions are installed on
the hosts **before** cross-wiring and created in databases **after** it, in both
`deploy_cluster.py` and `add_node.py`. Tests pin this ordering because getting
it wrong makes the cluster impossible to wire together.

**zodan script provenance.** The zodan/zodremove SQL must come from the branch
or tag the node's Spock was actually built from, not from the cluster's major —
a v5.0.5 cluster needs v5.0.5's script. `spock_management.stage_zodan` tries the
source-build checkout (comparing commits, since a tag leaves a detached HEAD),
then GitHub, then the bundled copy in `configuration/spock/`.

**Extension node identity.** `lolor.node` / `snowflake.node` must differ per
node and nothing detects a clash until generated ids collide. The value comes
from the number in the node's *name*, not its position — remove `n2` and `n3`
would slide into a number another node already uses. And the GUC does not exist
until the library is loaded, so it is set by `LOAD` + `ALTER SYSTEM` in one psql
session (`pg_server_management.psql_session`); `CREATE EXTENSION` alone does not
load it.

**Patroni's live config.** `bootstrap.dcs` is read once, when a scope is
created. Anything afterwards — permanent slots, synchronous settings — goes
through `patronictl edit-config`. Changes to survivors of a removal use
`reload`, never `restart`: the cluster stays up throughout.

**Version strings.** The minor and the pre-release are independent: `19`,
`19.0`, `19beta3` and `19.0beta3` are all real. Patterns that treat them as
alternatives reject `19.0beta3`, which is what a cluster on a pre-release
reports.

**Package names differ per family and neither is derivable from the other.**
`platform_detect.py` and `pg_extensions.ExtensionSpec.package` own this. RHEL
appends the major (`pgedge-lolor_17`), Debian puts the server in the middle
(`pgedge-postgresql-17-lolor`). ACE is version-independent: `pgedge-ace` on both.

## Testing model

`conftest.FakeExecutor(replies)` answers by **command substring** — first match
wins, unmatched commands succeed with empty output — and records everything in
`.commands`. Tests assert on the commands that *would* have run.

Two things catch people out:

- Commands are shell-quoted by the time they reach the executor, so
  `SELECT spock.sub_drop('x')` appears as `...sub_drop('"'"'x'"'"')`. Assert on
  fragments (`"sub_drop" in c and "x" in c`), not on the literal SQL.
- Functions that poll (`wait_for_subscriptions`, `wait_for_removal`) use real
  clocks. Patch `time.sleep` *and* pass a short timeout, or the test hangs.

`tests/test_deploy_script.py` runs the real `pg_deploy_cluster.sh` with its
`exec` hand-offs stubbed to `echo`, driving the prompts from a **positional**
answer stream built by `deployment_answers()`. Adding or reordering a prompt
breaks every test in that file until the helper is updated to match — that is
intentional, and the helper is the place to fix it.

## Skills in this repository

`.claude/skills/spock-pr-on-cluster/` validates a pgEdge/spock pull request
against the cluster this tool has deployed, using `spock rebuild` to put the
change on every node and the topology's own HA as test surface. It is the
in-cluster counterpart of the sandbox `spock-pr-validation` skill, and refuses
a cluster whose Spock major does not match the PR's base branch.

`.claude/skills/failover-deep-test/` is the exploratory counterpart of the
`failover` health-check suite: eleven scenarios on the deployed cluster
(idle-primary slot copies, failover under pgbench load, the promoted standby
serving the mesh, the switchover guard, two failovers in a row, a postmaster
crash, standby loss under strict sync, restore), each finding filed as a Spock
bug, a bug in this tool, or a topology limitation. Disruptive, by request only.

`.claude/skills/spock-deep-test/` does the same for the multi-master layer,
starting where the in-tree `spock` suite stops: DDL and DML from every node,
the four conflict shapes and `spock.resolutions`, delta-apply, Snowflake,
no-PK and partitioned tables, pgbench from every node at once, a peer down
while the others write, apply-side exceptions, repset filtering, repair and
read-only mode, resync, restore. Never together with `failover-deep-test`.

`lolor-deep-test`, `snowflake-deep-test` and `ace-deep-test` follow the same
shape for the optional components: each starts where the component's health
check stops (lolor's API, big and many objects, concurrent updates and
unlink races, the drop guard; Snowflake's identity bits, uniqueness under
concurrent inserts from every node, a tight loop, sequence conversion, a
promoted standby; ACE's detection of every planted divergence by both the
host binary and this tool's `diff`, scoping, repair semantics, schema and
repset drift, a big table). All five skills report node names only and file
each finding as a component bug, a bug in this tool, or a topology limitation.

`--health_check <components> --pr <link>` (`deployment/pr_validation.py`)
is the laptop-side driver for a cluster *already deployed from the PR's
branch*: it rebuilds nothing, runs the suites, then starts `claude -p` with a
prompt naming both skills and an allow-list of this repository's scripts
only, and reads the JSON verdict line Claude is told to end with. `decide`
makes ALL GOOD require both halves. If you are the Claude that prompt
started, the cluster is the harness: no rebuild, no `pg_ctl`, drop what you
create.

## Repository state

Feature work has been split across several long-lived branches (`gui-interface`
carries the Flask add-node/deploy forms, `patroni-sync`, `test-framework`,
`manual-build-deployment`). Files present on one are absent on another —
`dashboard/node_api.py` exists only on `gui-interface`. Check which branch a
file lives on before assuming an edit applies everywhere.
