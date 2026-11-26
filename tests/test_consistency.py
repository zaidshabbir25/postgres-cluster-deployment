#!/usr/bin/env python3
"""What an ACE deep test on a live cluster taught the tool's own `diff`.

Seven findings, each pinned here: a column named `t` made the row hash
hash that column alone; `--no-truncate` promised a merge that Spock's resync
cannot do; the drill-down silently stopped at 5000 rows; a last row ending
in NULL lost a field to strip(); the ace health check failed on a schema with
no tables; its workspace was root's; and a node that could not be read was
reported as a node with every object missing.
"""

from aspects import consistency
from aspects import pg_server_management as pg
from aspects.cluster_model import ClusterPlan, Host, Node, ROLE_SPOCK
from aspects.health_check import runner as regression
from tests.conftest import FakeExecutor
from tests.test_health_check_suites import plan_with, ready


def node(name, port):
    return Node(name=name, role=ROLE_SPOCK, host="local", address="10.0.1.10",
                pg_port=port, restapi_port=8000 + port % 100,
                data_dir=f"/var/lib/pgedge/{name}", scope=f"pgedge-{name}",
                config_file="c", pgpass_file="p", bin_dir="/usr/pgsql-17/bin")


def two_nodes():
    return ClusterPlan(cluster_name="pgedge", db_password="pw",
                       hosts=[Host(name="local", address="10.0.1.10")],
                       nodes=[node("n1", 5432), node("n2", 5433)])


SEP = "\x1f"


def per_node(replies_by_port):
    """One fake per node, told apart by the port in the psql command."""
    class Executor(FakeExecutor):
        def _answer(self, command):
            self.commands.append(command)
            for port, replies in replies_by_port.items():
                if f"-p {port} " in command:
                    for fragment, reply in replies.items():
                        if fragment in command:
                            return reply
            for fragment, reply in self.replies.items():
                if fragment in command:
                    return reply
            return True, ""
    executor = Executor()
    return lambda n: executor, executor


# ---------------------------------------------------------------------------
# 1. the row hash covers the row, whatever the columns are called
# ---------------------------------------------------------------------------


def test_the_row_is_hashed_under_an_alias_no_column_can_shadow():
    plan = two_nodes()
    executor_for, executor = per_node({})
    executor.replies = {"pg_index": (True, "id\n")}

    consistency.table_diff(executor_for, plan, "public.rows")

    hashes = [c for c in executor.commands if "string_agg" in c]
    assert hashes
    assert all(f"md5({consistency.ROW}::text)" in c for c in hashes)
    assert all("md5(t::text)" not in c for c in hashes)
    assert consistency.ROW.startswith("_"), "not a name a column would carry"


# ---------------------------------------------------------------------------
# 3. a bucket past the cap is said to be
# ---------------------------------------------------------------------------


def test_a_capped_bucket_is_reported_not_passed_off_as_complete(monkeypatch):
    monkeypatch.setattr(consistency, "MAX_ROWS_PER_BUCKET", 2)
    plan = two_nodes()
    executor_for, executor = per_node({
        5432: {"string_agg": (True, f"0{SEP}3{SEP}aaa\n"),
               "LIMIT": (True, f"1{SEP}x\n2{SEP}y\n3{SEP}z\n")},
        5433: {"string_agg": (True, f"0{SEP}3{SEP}bbb\n"),
               "LIMIT": (True, f"1{SEP}x\n2{SEP}Y\n3{SEP}z\n")},
    })
    executor.replies = {"pg_index": (True, "id\n")}

    ok, report = consistency.table_diff(executor_for, plan, "public.rows", buckets=1)

    assert ok is False
    assert report["mismatches"]["n2"]["capped_buckets"] == ["0"]
    assert any("incomplete" in p and "--buckets 16" in p for p in report["problems"])
    assert any("LIMIT 3" in c for c in executor.commands), "asks for one past the cap"


# ---------------------------------------------------------------------------
# 2. --no-truncate merges, because Spock's resync cannot
# ---------------------------------------------------------------------------


def merge_cluster(diff_keys_only_on_source=("7",), different=("9",), extra=("42",)):
    plan = two_nodes()
    source_rows = "".join(f"{k}{SEP}s\n" for k in sorted(
        set(diff_keys_only_on_source) | set(different) | {"1"}))
    target_rows = "".join(f"{k}{SEP}{'t' if k in different else 's'}\n"
                          for k in sorted(set(different) | set(extra) | {"1"}))
    executor_for, executor = per_node({
        5432: {"string_agg": (True, f"0{SEP}3{SEP}aaa\n"),
               "LIMIT": (True, source_rows)},
        5433: {"string_agg": (True, f"0{SEP}3{SEP}bbb\n"),
               "LIMIT": (True, target_rows),
               "format_type": (True, f"id{SEP}bigint{SEP}{SEP}\nv{SEP}text{SEP}{SEP}\n")},
    })
    executor.replies = {"pg_index": (True, "id\n")}
    return plan, executor_for, executor


def test_a_merge_writes_only_the_rows_the_diff_named_and_keeps_the_targets_extras():
    plan, executor_for, executor = merge_cluster()

    ok, report = consistency.table_repair(executor_for, plan, "public.rows", "n1",
                                          truncate=False)

    assert ok is True, report
    assert not executor.ran("sub_resync_table"), "Spock's COPY would hit the pkey"
    merge = next(c for c in executor.commands if "dblink" in c)
    assert "-p 5433" in merge, "written on the target"
    assert "repair_mode(true)" in merge, "kept off the mesh"
    assert "ON CONFLICT (id) DO UPDATE SET v = EXCLUDED.v" in merge
    assert "'7'" in merge and "'9'" in merge, "missing and different rows"
    assert "'42'" not in merge, "the target's own row is not touched"
    assert "42 row(s) only on n2 kept" not in report["results"][0]["output"]
    assert "1 row(s) only on n2 kept" in report["results"][0]["output"]
    assert "2 row(s) merged" in report["results"][0]["output"]


def test_a_target_that_already_agrees_is_left_alone():
    plan = two_nodes()
    executor_for, executor = per_node({
        5432: {"string_agg": (True, f"0{SEP}1{SEP}same\n")},
        5433: {"string_agg": (True, f"0{SEP}1{SEP}same\n")},
    })
    executor.replies = {"pg_index": (True, "id\n")}

    ok, report = consistency.table_repair(executor_for, plan, "public.rows", "n1",
                                          truncate=False)

    assert ok is True
    assert "nothing merged" in report["results"][0]["output"]
    assert not executor.ran("dblink") and not executor.ran("sub_resync_table")


def test_a_merge_the_diff_could_not_list_fully_is_refused(monkeypatch):
    monkeypatch.setattr(consistency, "MAX_ROWS_PER_BUCKET", 1)
    plan, executor_for, executor = merge_cluster()

    ok, report = consistency.table_repair(executor_for, plan, "public.rows", "n1",
                                          truncate=False)

    assert ok is False
    assert any("merge refused" in p for p in report["problems"])
    assert not executor.ran("dblink")


def test_a_truncating_repair_still_goes_through_spock():
    plan, executor_for, executor = merge_cluster()

    consistency.table_repair(executor_for, plan, "public.rows", "n1", truncate=True)

    assert executor.ran("sub_resync_table") and not executor.ran("dblink")


# ---------------------------------------------------------------------------
# 4. a last row ending in NULL keeps its field
# ---------------------------------------------------------------------------


def test_a_trailing_null_in_the_last_row_survives_parsing():
    executor = FakeExecutor(replies={"SELECT": (True, f"1{SEP}a\n2{SEP}\n")})

    result = pg.rows(executor, "/usr/pgsql-17/bin", 5432, "postgres", "SELECT 1")

    assert result == [["1", "a"], ["2", ""]]


def test_blank_lines_are_still_skipped():
    executor = FakeExecutor(replies={"SELECT": (True, f"\n1{SEP}a\n  \n")})

    assert pg.rows(executor, "/usr/pgsql-17/bin", 5432, "postgres", "SELECT 1") \
        == [["1", "a"]]


# ---------------------------------------------------------------------------
# 7. a node that cannot be read is not a node with nothing in it
# ---------------------------------------------------------------------------


def test_an_unreadable_node_is_reported_as_unreadable_not_as_missing_everything():
    plan = two_nodes()
    executor_for, executor = per_node({
        5432: {"information_schema.columns": (True, f"rows.id{SEP}bigint:-:64:NO:-\n"),
               "pg_indexes": (True, f"rows_pkey{SEP}CREATE UNIQUE INDEX ...\n"),
               "pg_constraint": (True, f"rows_pkey{SEP}PRIMARY KEY (id)\n")},
        5433: {"information_schema.columns": (False, "connection refused"),
               "pg_indexes": (False, "connection refused"),
               "pg_constraint": (False, "connection refused")},
    })

    ok, report = consistency.schema_diff(executor_for, plan)

    assert ok is False
    assert report["problems"] == ["n2: cannot read the schema — not compared"]
    assert not any("missing" in p for p in report["problems"])


# ---------------------------------------------------------------------------
# 5 + 6. the ace health check
# ---------------------------------------------------------------------------


def test_nothing_to_compare_is_not_a_failed_check():
    plan = plan_with()
    executor = ready(**{
        "command -v ace": (True, ""),
        "schema-diff": (False, "ERRO no tables found in schema public"),
        "repset-diff": (False, "ERRO no tables found in replication set default"),
    })

    result = regression.suite_for("ace").run(executor, plan, plan.node("n1"), "installed")

    assert result.status == regression.PASSED, result.detail
    assert result.tests["failed"] == 0


def test_a_real_mismatch_still_fails():
    plan = plan_with()
    executor = ready(**{"command -v ace": (True, ""),
                        "spock-diff": (True, "nodes DO NOT MATCH")})

    result = regression.suite_for("ace").run(executor, plan, plan.node("n1"), "installed")

    assert result.status == regression.FAILED


def test_the_workspace_belongs_to_the_database_user_who_runs_ace():
    plan = plan_with()
    executor = ready(**{"command -v ace": (True, "")})

    regression.suite_for("ace").run(executor, plan, plan.node("n1"), "installed")

    workspace = f"{regression.REGRESSION_ROOT}/ace"
    assert any(f"chown -R {plan.db_user} {workspace}" in c for c in executor.commands)
