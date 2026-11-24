#!/usr/bin/env python3
"""Validating a Spock pull request on the running cluster.

`--health_check spock --pr <link>` rebuilds nothing: the cluster is taken as
deployed from the branch under review. The suites run, then Claude Code on
the laptop reads the change and tries to break it through this
repository's own commands. The tests pin what is refused before anything
runs, that nothing is rebuilt, what Claude is told, how its verdict is read,
and that ALL GOOD needs both halves to be good.
"""

import pytest

from aspects import state
from deployment import health_check, pr_validation, spock_rebuild

PR = "https://github.com/pgEdge/spock/pull/626"


# ---------------------------------------------------------------------------
# what --pr accepts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    PR, PR + "/files", PR + "#discussion_r1", "github.com/pgEdge/spock/pull/626",
    "626", "#626", "pgEdge/spock#626", "pull/626/head", "refs/pull/626/head",
])
def test_every_spelling_of_the_pull_request_is_the_same_change(text):
    change = pr_validation.parse_change(text)

    assert change.number == 626
    assert change.is_pull_request and change.label == "PR #626"
    assert change.url == PR
    assert change.slug == "pr-626"


@pytest.mark.parametrize("text", ["v5_STABLE", "4a4dd44f6a", "v5.0.11"])
def test_a_commit_or_branch_is_accepted_too(text):
    change = pr_validation.parse_change(text)

    assert change.ref == text and not change.is_pull_request


@pytest.mark.parametrize("url,ref", [
    ("https://github.com/pgEdge/spock/commit/d2fc8d5d8ada2ea2fb6699540dc480b10426a44e",
     "d2fc8d5d8ada2ea2fb6699540dc480b10426a44e"),
    ("https://github.com/pgEdge/spock/tree/v5_STABLE", "v5_STABLE"),
    ("https://github.com/pgEdge/spock/commits/main", "main"),
])
def test_a_commit_or_branch_link_names_its_ref(url, ref):
    change = pr_validation.parse_change(url)

    assert change.ref == ref and not change.is_pull_request
    assert change.url.count("https://") == 1, "no link inside a link"
    assert len(change.slug) <= 12 or change.slug == ref


def test_a_commit_link_in_another_repository_is_refused():
    with pytest.raises(ValueError, match="only pgEdge/spock"):
        pr_validation.parse_change("https://github.com/pgEdge/lolor/commit/abc123def456")


def test_a_pull_request_to_another_repository_is_refused():
    with pytest.raises(ValueError, match="only pgEdge/spock"):
        pr_validation.parse_change("https://github.com/pgEdge/lolor/pull/12")


# ---------------------------------------------------------------------------
# the Spock major the PR targets
# ---------------------------------------------------------------------------


def test_a_pr_against_main_cannot_be_judged_by_a_spock_5_cluster(deployed_plan):
    deployed_plan.spock_major = "50"

    problem = pr_validation.major_mismatch(deployed_plan, "main")

    assert "Spock 6" in problem and "cannot be carrying the change" in problem


def test_a_pr_against_the_stable_branch_fits_a_spock_5_cluster(deployed_plan):
    deployed_plan.spock_major = "50"

    assert pr_validation.major_mismatch(deployed_plan, "v5_STABLE") == ""


def test_a_topic_base_branch_is_not_a_reason_to_refuse(deployed_plan):
    assert pr_validation.major_mismatch(deployed_plan, "feature/foo") == ""


# ---------------------------------------------------------------------------
# reading Claude's answer
# ---------------------------------------------------------------------------


def test_the_verdict_is_the_last_json_line():
    stdout = ('Some prose.\n{"not": "this"}\n'
              '{"verdict": "pass", "summary": "fine", "findings": [], '
              '"scenarios": 7}\n')

    verdict = pr_validation.parse_claude_verdict(stdout)

    assert verdict["verdict"] == "PASS" and verdict["scenarios"] == 7


def test_a_fenced_verdict_line_still_counts():
    stdout = '```json\n{"verdict": "FAIL", "summary": "broke"}\n```\n'

    assert pr_validation.parse_claude_verdict(stdout)["verdict"] == "FAIL"


def test_no_verdict_line_is_none():
    assert pr_validation.parse_claude_verdict("I ran out of turns.") is None


# ---------------------------------------------------------------------------
# deciding
# ---------------------------------------------------------------------------


def suites(**status):
    return {"outcome": "x", "log_dir": "/l",
            "results": [{"component": c, "status": s,
                         "tests": {"total": 8, "passed": 8 if s == "passed" else 7,
                                   "failed": 0 if s == "passed" else 1}}
                        for c, s in status.items()]}


def review(verdict, *findings, cluster="pgedge", nodes=("n1", "n2")):
    return {"verdict": verdict, "summary": "s", "findings": list(findings),
            "cluster": cluster, "nodes": list(nodes)}


def test_all_good_needs_suites_passed_and_nothing_found():
    verdict, problems = pr_validation.decide(suites(spock="passed"),
                                             review("PASS"))

    assert verdict == pr_validation.ALL_GOOD and problems == []


def test_a_failed_suite_is_a_fail_whatever_claude_says():
    verdict, problems = pr_validation.decide(suites(spock="failed"),
                                             review("PASS"))

    assert verdict == pr_validation.FAIL
    assert problems == ["suite spock failed: 7/8 passed, 1 failed"]


def test_claudes_findings_are_each_reported():
    verdict, problems = pr_validation.decide(
        suites(spock="passed"), review("FAIL", "row missing on n3",
                                       "ERROR in n2 log"))

    assert verdict == pr_validation.FAIL
    assert "finding: row missing on n3" in problems
    assert "finding: ERROR in n2 log" in problems


def test_no_verdict_from_claude_is_inconclusive_not_good():
    verdict, problems = pr_validation.decide(suites(spock="passed"), None)

    assert verdict == pr_validation.INCONCLUSIVE


def test_everything_skipped_is_not_all_good():
    verdict, problems = pr_validation.decide(suites(spock="skipped"),
                                             review("PASS"))

    assert verdict == pr_validation.INCONCLUSIVE


# ---------------------------------------------------------------------------
# it must have been this cluster
# ---------------------------------------------------------------------------


def test_a_verdict_about_another_cluster_is_not_accepted(deployed_plan):
    verdict, problems = pr_validation.decide(
        suites(spock="passed"), review("PASS", cluster="sandbox"),
        deployed_plan)

    assert verdict == pr_validation.INCONCLUSIVE
    assert any("not accepted" in p and deployed_plan.cluster_name in p
               for p in problems)


def test_a_verdict_naming_nodes_this_cluster_lacks_is_not_accepted(deployed_plan):
    verdict, problems = pr_validation.decide(
        suites(spock="passed"),
        review("PASS", cluster=deployed_plan.cluster_name, nodes=("n1", "n9")),
        deployed_plan)

    assert verdict == pr_validation.INCONCLUSIVE
    assert any("n9" in p for p in problems)


def test_a_verdict_that_touched_no_node_is_not_accepted(deployed_plan):
    verdict, problems = pr_validation.decide(
        suites(spock="passed"),
        review("PASS", cluster=deployed_plan.cluster_name, nodes=()),
        deployed_plan)

    assert verdict == pr_validation.INCONCLUSIVE
    assert any("no node" in p for p in problems)


def test_a_pass_on_this_cluster_and_its_nodes_is_all_good(deployed_plan):
    verdict, problems = pr_validation.decide(
        suites(spock="passed"),
        review("PASS", cluster=deployed_plan.cluster_name, nodes=("n1",)),
        deployed_plan)

    assert verdict == pr_validation.ALL_GOOD and problems == []


def test_a_fail_elsewhere_is_still_not_a_verdict_about_this_cluster(deployed_plan):
    """A FAIL from a sandbox would otherwise be reported as this cluster's."""
    verdict, problems = pr_validation.decide(
        suites(spock="passed"), review("FAIL", "boom", cluster="sandbox"),
        deployed_plan)

    assert verdict == pr_validation.INCONCLUSIVE
    assert not any(p.startswith("finding") for p in problems)


# ---------------------------------------------------------------------------
# the whole run
# ---------------------------------------------------------------------------


class _Logger:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.steps = []

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def cluster(monkeypatch, deployed_plan, tmp_path):
    plan = deployed_plan
    plan.spock_major = "60"
    journal = []

    def versions(cluster_name, db_password=None):
        journal.append("versions")
        return [{"node": n.name, "loaded": "6.0.0-devel", "extension": "6.0.0",
                 "available": "6.0.0", "checksum": "abc"} for n in plan.nodes]

    def suites_run(cluster_name, components, run_logger=None, **_):
        journal.append("suites")
        return {"outcome": "succeeded", "log_dir": str(run_logger.root),
                "results": [{"component": c, "status": "passed",
                             "tests": {"total": 8, "passed": 8}}
                            for c in components]}

    def claude(prompt, cwd, log_dir, **_):
        journal.append("claude")
        (log_dir / "claude-report.md").write_text("# review\n")
        return 0, ('done\n{"verdict": "PASS", "summary": "nothing broke", '
                   '"findings": [], "scenarios": 9, "cluster": "'
                   + plan.cluster_name + '", "nodes": ["n1", "n2"]}\n'), ""

    monkeypatch.setattr(state, "load", lambda *a, **k: (plan, {}))
    monkeypatch.setattr(state, "resolve_password", lambda p, explicit=None: "s")
    monkeypatch.setattr(spock_rebuild, "versions", versions)
    monkeypatch.setattr(spock_rebuild, "rebuild",
                        lambda *a, **k: pytest.fail("rebuilt!"))
    monkeypatch.setattr(health_check, "run", suites_run)
    monkeypatch.setattr(pr_validation, "RunLogger",
                        lambda run_id, **k: _Logger(tmp_path / run_id))
    return plan, journal, claude


def on_main(number):
    return {"title": "Fix a thing", "base": "main", "head_sha": "4a4dd44",
            "state": "open", "merged": False, "draft": False}


def test_suites_then_claude_and_nothing_rebuilt(cluster):
    plan, journal, claude = cluster

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main,
                               claude=claude)

    assert result["outcome"] == "succeeded", result
    assert result["verdict"] == pr_validation.ALL_GOOD
    assert journal == ["versions", "suites", "claude"]
    assert result["review"]["scenarios"] == 9
    assert result["claude_report"].endswith("claude-report.md")


def test_claude_is_told_not_to_rebuild_and_which_skills_to_follow(cluster):
    plan, journal, _ = cluster
    prompts = []

    def claude(prompt, cwd, log_dir, **_):
        prompts.append(prompt)
        return 0, ('{"verdict": "PASS", "summary": "ok", "cluster": "'
                   + plan.cluster_name + '", "nodes": ["n1"]}\n'), ""

    pr_validation.run("pgedge", ["spock", "lolor"], PR, lookup=on_main,
                      claude=claude)

    prompt = prompts[0]
    assert "ALREADY DEPLOYED" in prompt and "Do not rebuild" in prompt
    assert "spock-pr-validation" in prompt
    assert "spock-pr-on-cluster/SKILL.md" in prompt
    assert PR in prompt and "Fix a thing" in prompt
    assert "- spock: passed" in prompt and "- lolor: passed" in prompt
    assert "n1" in prompt and "10.0.1.11" not in prompt, "node names, no addresses"
    assert "Never pg_ctl" in prompt
    assert '"verdict"' in prompt, "told how to answer"
    assert f"--cluster {plan.cluster_name}" in prompt, "every call pinned"
    assert "confirm the nodes are exactly n1, n2" in prompt
    assert "Do not deploy another cluster" in prompt and "initdb" in prompt
    assert f'"cluster": "{plan.cluster_name}"' in prompt


def test_the_inventory_path_travels_into_every_call(cluster):
    plan, journal, _ = cluster
    prompts = []

    def claude(prompt, cwd, log_dir, **_):
        prompts.append(prompt)
        return 0, ('{"verdict": "PASS", "summary": "ok", "cluster": "'
                   + plan.cluster_name + '", "nodes": ["n1"]}\n'), ""

    pr_validation.run("pgedge", ["spock"], PR, lookup=on_main, claude=claude,
                      inventory_path="configuration/inventory.local.json")

    assert (f"--cluster {plan.cluster_name} --inventory "
            f"configuration/inventory.local.json") in prompts[0]


def test_a_review_that_ran_somewhere_else_is_rejected_end_to_end(cluster):
    plan, journal, _ = cluster

    def claude(prompt, cwd, log_dir, **_):
        return 0, ('{"verdict": "PASS", "summary": "all fine in docker", '
                   '"cluster": "sandbox", "nodes": ["n1", "n2"]}\n'), ""

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main,
                               claude=claude)

    assert result["verdict"] == pr_validation.INCONCLUSIVE
    assert any("not accepted" in p for p in result["problems"])


def test_claudes_findings_make_it_a_fail_with_each_one_listed(cluster):
    plan, journal, _ = cluster

    def claude(prompt, cwd, log_dir, **_):
        return 0, ('{"verdict": "FAIL", "summary": "row lost on failover", '
                   '"findings": ["n3 never got the row", "TRAP in n2 log"], '
                   '"cluster": "' + plan.cluster_name
                   + '", "nodes": ["n1", "n2"]}\n'), ""

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main,
                               claude=claude)

    assert result["verdict"] == pr_validation.FAIL
    assert "finding: n3 never got the row" in result["problems"]
    assert "finding: TRAP in n2 log" in result["problems"]
    text = open(result["report"], encoding="utf-8").read()
    assert text.startswith("# PR #626 on cluster") and "**FAIL**" in text
    assert "TRAP in n2 log" in text


def test_claude_giving_no_verdict_is_inconclusive_with_the_log_named(cluster):
    plan, journal, _ = cluster

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main,
                               claude=lambda *a, **k: (1, "ran out", "boom"))

    assert result["outcome"] == "succeeded"
    assert result["verdict"] == pr_validation.INCONCLUSIVE
    assert any("claude-stdout.log" in w for w in result["warnings"])


def test_a_pr_for_the_wrong_major_stops_before_anything_runs(cluster):
    plan, journal, claude = cluster
    plan.spock_major = "50"

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main,
                               claude=claude)

    assert result["outcome"] == "failed"
    assert "cannot be carrying the change" in result["error"]
    assert journal == []


def test_github_being_unreachable_is_a_warning_not_a_stop(cluster):
    plan, journal, claude = cluster

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=lambda n: None,
                               claude=claude)

    assert result["verdict"] == pr_validation.ALL_GOOD
    assert any("base branch is unknown" in w for w in result["warnings"])


def test_nodes_on_different_builds_are_refused(cluster, monkeypatch):
    plan, journal, claude = cluster
    monkeypatch.setattr(spock_rebuild, "versions", lambda *a, **k: [
        {"node": "n1", "loaded": "6.0.0-a", "extension": "6.0.0",
         "available": "6.0.0", "checksum": "1"},
        {"node": "n2", "loaded": "6.0.0-b", "extension": "6.0.0",
         "available": "6.0.0", "checksum": "2"}])

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main,
                               claude=claude)

    assert result["outcome"] == "failed"
    assert "different Spock builds" in result["error"]
    assert "suites" not in journal


def test_a_failed_suite_is_reported_even_when_claude_is_happy(cluster,
                                                               monkeypatch):
    plan, journal, claude = cluster
    monkeypatch.setattr(health_check, "run", lambda *a, run_logger=None, **k: {
        "outcome": "failed", "log_dir": str(run_logger.root),
        "results": [{"component": "spock", "status": "failed",
                     "detail": "2 of 8 cases differ", "tests": {}}]})

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main,
                               claude=claude)

    assert result["verdict"] == pr_validation.FAIL
    assert "suite spock failed: 2 of 8 cases differ" in result["problems"]


def test_no_review_runs_only_the_suites(cluster):
    plan, journal, _ = cluster

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main,
                               review=False,
                               claude=lambda *a, **k: pytest.fail("claude ran"))

    assert journal == ["versions", "suites"]
    assert result["verdict"] == pr_validation.ALL_GOOD
    assert any("review skipped" in w for w in result["warnings"])


def test_claude_missing_from_this_machine_is_said_plainly(cluster, monkeypatch):
    plan, journal, _ = cluster
    monkeypatch.setattr(pr_validation, "claude_available", lambda *a: None)

    result = pr_validation.run("pgedge", ["spock"], PR, lookup=on_main)

    assert result["outcome"] == "failed"
    assert "not on this machine's PATH" in result["error"]
    assert journal == ["versions", "suites"], "the suites still ran"


# ---------------------------------------------------------------------------
# what Claude may touch
# ---------------------------------------------------------------------------


class FakePopen:
    """A claude -p that emits a scripted stream-json conversation."""

    lines = [
        '{"type":"system","subtype":"init"}\n',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
        '"input":{"command":"./pg_cluster_ctl.sh --cluster pgedge node list"}}]}}\n',
        'not json at all\n',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}\n',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Write",'
        '"input":{"file_path":"/l/claude-report.md"}}]}}\n',
        '{"type":"result","result":"done\\n{\\"verdict\\": \\"PASS\\"}",'
        '"is_error":false,"num_turns":3,"total_cost_usd":1.25,"duration_ms":5}\n',
    ]
    seen = {}

    def __init__(self, argv, **kw):
        import io
        FakePopen.seen = dict(argv=argv, **kw)
        self.stdout = io.StringIO("".join(self.lines))

    def wait(self):
        return 0

    def kill(self):
        pass


def test_claude_is_allowed_the_scripts_and_github_and_nothing_wider(
        monkeypatch, tmp_path):
    monkeypatch.setattr(pr_validation.subprocess, "Popen", FakePopen)

    pr_validation.run_claude("do it", tmp_path, tmp_path, command="claude")

    argv = FakePopen.seen["argv"]
    assert argv[:3] == ["claude", "-p", "do it"]
    assert "stream-json" in argv and "--verbose" in argv
    assert FakePopen.seen["stdin"] is pr_validation.subprocess.DEVNULL, \
        "never waits on a terminal"
    allowed = argv[argv.index("--allowedTools") + 1:]
    assert "Bash(./pg_cluster_ctl.sh node list:*)" in allowed
    assert "Bash(./pg_cluster_ctl.sh service switchover:*)" in allowed
    assert "Bash(./pg_cluster_ctl.sh diff:*)" in allowed
    assert "Bash(gh api:*)" in allowed
    assert "Bash(./pg_cluster_ctl.sh:*)" not in allowed, "no blanket ctl"
    for refused in ("node add", "node remove", "node ssh", "spock rebuild",
                    "package", "db create", "db drop"):
        assert not any(refused in a for a in allowed), refused
    assert not any(a.startswith("Bash(./pg_deploy_cluster.sh")
                   and "--health_check" not in a for a in allowed), \
        "no deploy, no cleanup"
    assert not any(a in ("Bash", "Bash(*)") for a in allowed)
    assert not any("ssh" in a or "pg_ctl" in a or "systemctl" in a
                   for a in allowed)


def test_every_tool_call_is_shown_while_claude_works(monkeypatch, tmp_path):
    """An hour of silence looks like a hang; the console must show each
    command as Claude runs it, and the final text must be the result."""
    monkeypatch.setattr(pr_validation.subprocess, "Popen", FakePopen)
    shown = []

    code, text, stderr = pr_validation.run_claude(
        "do it", tmp_path, tmp_path, command="claude", progress=shown.append)

    assert code == 0
    assert any("./pg_cluster_ctl.sh --cluster pgedge node list" in l for l in shown)
    assert any("Write /l/claude-report.md" in l for l in shown)
    assert any("finished: 3 turn(s)" in l for l in shown)
    assert pr_validation.parse_claude_verdict(text)["verdict"] == "PASS"
    assert (tmp_path / "claude-stream.jsonl").read_text().count("\n") == \
        len(FakePopen.lines), "raw stream kept"


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def _args(**over):
    import types
    base = dict(cluster=None, components="spock", db_password=None, node=None,
                stop_on_failure=False, install_requirements=False, tests=None,
                test_timeout=0, inventory=None, json=False, pr=PR,
                no_review=False)
    base.update(over)
    return types.SimpleNamespace(**base)


def test_the_cli_routes_pr_to_the_validation(monkeypatch):
    from deployment import cli

    seen = {}
    monkeypatch.setattr(cli.state, "latest_cluster", lambda: "pgedge")
    monkeypatch.setattr(cli.health_check, "run",
                        lambda **k: pytest.fail("plain health check ran"))
    monkeypatch.setattr(cli.pr_validation, "run", lambda c, comps, change, **k:
                        seen.update(cluster=c, ref=change.ref, **k)
                        or {"outcome": "succeeded", "verdict": "ALL GOOD",
                            "log_dir": "/l", "problems": [], "warnings": []})

    assert cli.cmd_health_check(_args(no_review=True)) == cli.EXIT_OK
    assert seen["ref"] == "pull/626/head" and seen["review"] is False


def test_the_cli_exit_code_follows_the_verdict(monkeypatch):
    from deployment import cli

    monkeypatch.setattr(cli.state, "latest_cluster", lambda: "pgedge")
    monkeypatch.setattr(cli.pr_validation, "run", lambda *a, **k: {
        "outcome": "succeeded", "verdict": "FAIL", "log_dir": "/l",
        "problems": ["finding: x"], "warnings": []})

    assert cli.cmd_health_check(_args()) == cli.EXIT_FAIL
