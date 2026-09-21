# Tests

Unit tests for the deployment logic. Nothing here contacts a machine, installs
a package or starts a service: every module reaches the outside world through
an executor, so a recording stand-in for that executor (`conftest.FakeExecutor`)
drives the whole thing and the tests assert on the commands that *would* have
run.

```bash
source venv/bin/activate          # or: python3 -m venv venv && pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest                            # ~230 tests, about two seconds
pytest tests/test_topology.py -v  # one area
```

## What each module covers

| file | area |
|------|------|
| `test_topology.py` | placement: one node per machine, ports per machine, standby placement, scopes, collisions |
| `test_etcd_management.py` | member selection, the URLs etcd advertises, config dialects, why a start failed |
| `test_patroni_management.py` | the rendered config, and every way a wait ends: leader, replica, dead unit, restart loop, foreign lock owner, unreachable DCS |
| `test_versions_and_auth.py` | version comparison, psql invocation, pg_hba and pgpass generation |
| `test_spock_management.py` | which zodan script a node gets and from where, extensions, the join verdict |
| `test_source_build.py` | prefixes per major, permissions, the Patroni venv, building one major alongside another |
| `test_ssh_executor.py` | command wrapping, stepping down to the database user, the environment a local command inherits |
| `test_add_node.py` | placement, ports, the PostgreSQL and Spock version rules, and the guards in `add()` |
| `test_inventory_and_state.py` | loading and validating the inventory, saving and reloading a cluster |
| `test_cli_and_cleanup.py` | flag parsing, `node add` dispatch, host cleanup, the confirmation prompt |
| `test_deploy_script.py` | `pg_deploy_cluster.sh` itself: the prompts, the flags they produce, and what each mode refuses |

`test_deploy_script.py` runs the real shell script with its `exec` hand-offs
stubbed out, so it exercises the prompt flow end to end and asserts on the
`deployment.cli` command line the answers produce. It needs `bash` and is
skipped without it.

## Fixtures

- `FakeExecutor(replies)` — scripted answers by command substring, with
  `ran()`/`count()` for assertions and `written`/`uploaded` for file transfers.
- `RecordingLogger` — a `RunLogger` stand-in with `said()`.
- `plan_factory` / `deployed_plan` — a planned cluster, and one as it looks
  after the hosts have been probed.
- `state_dir`, `inventory_file`, `logs_in_tmp` — keep state, inventories and
  run logs inside `tmp_path` rather than the repository.

## Adding a test

Most bugs this suite covers were a decision made from the wrong input — a port
counted per inventory entry rather than per machine, a script chosen by a stale
pin rather than by the node's Spock. When you fix one, the test that belongs
with it usually asserts on what was *sent to the host*, not on a return value:

```python
def test_something(deployed_plan):
    executor = FakeExecutor({"systemctl is-active": (True, "failed")})

    ok, _, detail = pm.wait_for_role(executor, node, ["Leader"], timeout=420)

    assert ok is False
    assert executor.count("sleep 5") <= 1      # it did not wait out the timeout
```
