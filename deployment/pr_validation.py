#!/usr/bin/env python3
"""Validate a Spock pull request on the cluster that is already running.

This is `--health_check spock --pr <link>`. The cluster is taken as deployed
from the branch under review — nothing is rebuilt. From the laptop:

  1. read the PR from GitHub, and refuse a cluster whose Spock major the PR
     does not target, since the suites would then be judging the wrong code;
  2. record what every node loads;
  3. run the named suites against the cluster;
  4. hand the PR to Claude Code on this machine: read the change, review it
     the way the spock-pr-validation skill does, and try to break it on the
     running cluster with this repository's own commands — scenarios on the
     mesh, under failover where a node has a standby, across every peer —
     then write a report with a verdict;
  5. say ALL GOOD when the suites passed and Claude found nothing, or report
     every failure and finding.

Claude never rebuilds, never changes configuration, never goes around
Patroni; it leaves the cluster as it found it. The prompt it gets says so,
and names the two skills it is to follow: the sandbox skill for how to read
and attack a change, the in-cluster skill for how to drive this cluster.
"""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aspects import source_build, state
from aspects.logging_setup import RunLogger, new_run_id
from deployment import health_check, spock_rebuild

SPOCK_REPO = "pgEdge/spock"
GITHUB_API = "https://api.github.com"

ALL_GOOD, FAIL, INCONCLUSIVE = "ALL GOOD", "FAIL", "INCONCLUSIVE"

#: The Claude Code executable, and how long one review may take.
CLAUDE_COMMAND = os.environ.get("CLAUDE_CMD", "claude")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_PR_TIMEOUT", "3600"))

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_CLUSTER_SKILL = REPO_ROOT / ".claude/skills/spock-pr-on-cluster/SKILL.md"
SANDBOX_SKILL = "spock-pr-validation"


class ValidationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# What the user asked for
# ---------------------------------------------------------------------------


@dataclass
class Change:
    """A Spock change to validate: a pull request, or a commit or branch."""

    ref: str
    number: int = None
    repo: str = SPOCK_REPO

    @property
    def is_pull_request(self):
        return self.number is not None

    @property
    def label(self):
        return f"PR #{self.number}" if self.is_pull_request else self.ref

    @property
    def slug(self):
        if self.is_pull_request:
            return f"pr-{self.number}"
        ref = self.ref[:12] if re.fullmatch(r"[0-9a-f]{12,40}", self.ref) else self.ref
        return re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-") or "ref"

    @property
    def url(self):
        if self.is_pull_request:
            return f"https://github.com/{self.repo}/pull/{self.number}"
        return f"https://github.com/{self.repo}/commit/{self.ref}"


_PR_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<repo>[^/\s]+/[^/\s#]+)"
    r"/pull/(?P<number>\d+)(?:[/?#].*)?$")
_PR_SHORT = re.compile(r"^(?:(?P<repo>[^/\s#]+/[^/\s#]+))?#(?P<number>\d+)$")
_PR_REF = re.compile(r"^(?:refs/)?pull/(?P<number>\d+)/(?:head|merge)$")
_REF_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<repo>[^/\s]+/[^/\s#]+)"
    r"/(?:commit|tree|commits)/(?P<ref>[^?#\s]+?)/?(?:[?#].*)?$")


def parse_change(value):
    """Turn what was typed after --pr into a Change.

    Accepted: a GitHub PR link, `#626`, `626`, `pgEdge/spock#626`,
    `pull/626/head`, or a commit or branch of Spock. A pull request in
    another repository is refused: the cluster runs Spock, and that is what
    the suites and the review are about.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("--pr needs a pull request link or number")

    for pattern in (_PR_URL, _PR_SHORT):
        match = pattern.match(text)
        if match:
            repo = match.group("repo") or SPOCK_REPO
            if repo.lower().rstrip("/").removesuffix(".git") != SPOCK_REPO.lower():
                raise ValueError(
                    f"{text} is a pull request to {repo}; only pgEdge/spock "
                    f"changes can be validated on the cluster")
            number = int(match.group("number"))
            return Change(ref=f"pull/{number}/head", number=number)

    match = _PR_REF.match(text)
    if match:
        number = int(match.group("number"))
        return Change(ref=f"pull/{number}/head", number=number)

    match = _REF_URL.match(text)
    if match:
        repo = match.group("repo")
        if repo.lower().removesuffix(".git") != SPOCK_REPO.lower():
            raise ValueError(
                f"{text} is in {repo}; only pgEdge/spock changes can be "
                f"validated on the cluster")
        return Change(ref=match.group("ref"))

    if text.isdigit():
        return Change(ref=f"pull/{int(text)}/head", number=int(text))

    if re.search(r"\s", text):
        raise ValueError(f"{text!r} is not a pull request link, number or git ref")
    return Change(ref=text)


# ---------------------------------------------------------------------------
# What the PR targets
# ---------------------------------------------------------------------------


def github_pull_request(number, repo=SPOCK_REPO, timeout=15):
    """The PR's title, base branch, head sha and state from GitHub, or None.

    Unauthenticated: the repository is public and one request per run is far
    inside the anonymous rate limit. Any failure — offline laptop, a rate
    limit after all, a PR that does not exist — returns None; the caller
    decides whether it can go on without knowing the base branch.
    """
    request = urllib.request.Request(
        f"{GITHUB_API}/repos/{repo}/pulls/{number}",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "postgres-cluster-deployment"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:                               # noqa: BLE001 - optional
        return None
    return {
        "title": data.get("title") or "",
        "base": (data.get("base") or {}).get("ref") or "",
        "head_sha": ((data.get("head") or {}).get("sha") or "")[:12],
        "state": data.get("state") or "",
        "merged": bool(data.get("merged")),
        "draft": bool(data.get("draft")),
    }


def major_of_branch(branch):
    """The Spock major a branch is developed on, or None for a topic branch."""
    for major, name in source_build.SPOCK_BRANCH_BY_MAJOR.items():
        if name == branch:
            return major
    return None


def major_mismatch(plan, base_branch):
    """A sentence saying why this cluster cannot be judging the PR, or ''."""
    wanted = major_of_branch(base_branch)
    if not wanted or str(plan.spock_major) == wanted:
        return ""
    return (f"the PR targets {base_branch}, which is Spock {wanted[0]}.x, "
            f"but this cluster runs spock{plan.spock_major}, so it cannot "
            f"be carrying the change. Deploy from the PR's branch first "
            f"(for example --topology 3-node-extensions-source with "
            f"--spock-branch <the PR's branch>)")


# ---------------------------------------------------------------------------
# Claude on the laptop
# ---------------------------------------------------------------------------


def claude_available(command=None):
    return shutil.which(command or CLAUDE_COMMAND)


def ctl_prefix(plan, inventory_path=None):
    """The flags every pg_cluster_ctl.sh call in the review must carry, so
    it reaches this cluster and not whichever was deployed last."""
    prefix = f"--cluster {plan.cluster_name}"
    if inventory_path:
        prefix += f" --inventory {inventory_path}"
    return prefix


def review_prompt(change, pr, plan, versions, suites, report_path,
                  inventory_path=None):
    """What Claude is asked to do. Node names only; no addresses or
    credentials, because the prompt is logged."""
    flags = ctl_prefix(plan, inventory_path)
    node_names = [n.name for n in plan.nodes]
    nodes = ", ".join(
        f"{n.name} ({'spock' if n.is_spock else 'standby of ' + str(getattr(n, 'leader', '') or '?')})"
        for n in plan.nodes)
    loaded = sorted({v.get("loaded") or "?" for v in versions.values()})
    suite_lines = "\n".join(
        f"  - {r['component']}: {r['status']} ({r.get('detail') or _counts(r)})"
        for r in suites.get("results", [])) or "  - none ran"

    about = [f"{'Pull request' if change.is_pull_request else 'Change'}: "
             f"{change.url}"]
    if pr:
        about.append(f"Title: {pr['title']}")
        about.append(f"Base branch: {pr['base']}; head {pr['head_sha']}; "
                     f"{pr['state']}" + (", merged" if pr["merged"] else ""))
    about = "\n".join(about)

    return f"""Validate a pgEdge/spock change on the cluster this repository has deployed.

{about}

THE CLUSTER IS THE ONE THIS REPOSITORY DEPLOYED with pg_deploy_cluster.sh:
cluster '{plan.cluster_name}', nodes {nodes}. It is ALREADY DEPLOYED FROM
THE BRANCH UNDER REVIEW and every node loads Spock {', '.join(loaded)}.
Do not rebuild Spock, do not run `spock rebuild`, do not install anything.
Do not deploy another cluster, start a sandbox, initdb, run docker, or test
on a local PostgreSQL: if this cluster cannot be reached, stop and answer
INCONCLUSIVE. Every `./pg_cluster_ctl.sh` and `./pg_cluster_status.sh` call
MUST carry `{flags}` so it reaches this cluster and not another saved one.
Before the first scenario run `./pg_cluster_ctl.sh {flags} node list` and
confirm the nodes are exactly {', '.join(node_names)}; if not, stop.

The component suites already ran on this build:
{suite_lines}

Your job is the review and the attempt to break the change, the parts no
suite does:

1. Understand the change the way the `{SANDBOX_SKILL}` skill's step 1 says:
   read the PR (title, body, files, review comments, linked issues) with
   `gh api` or `curl` against api.github.com, and write down the claimed
   bug, its trigger, what it touches (provider hook, output plugin, apply
   worker, SQL surface, catalog) and the tests it adds. Skip that skill's
   build, sandbox and two-node harness steps entirely: the cluster is the
   harness.
2. Drive the cluster only through this repository's commands, as
   {IN_CLUSTER_SKILL} describes (`./pg_cluster_ctl.sh node list`,
   `service status`, `spock sub-show-status`, `node command "<sql>" --sql
   --compare`, `diff all`, `spock lag`, `service logs`, `service switchover`,
   `pg_cluster_status.sh --repair-failover`). Never pg_ctl, never systemctl,
   never edit a config, never create roles or change pg_hba.
3. Try to break it, using that skill's step 5 and the sandbox skill's step 6
   edge cases: the PR's own trigger on one node verified on EVERY peer; the
   same under failover of a node that has a standby; the apply side, not
   only the origin; apply worker restart (sub-disable/sub-enable); inside
   BEGIN and multi-statement transactions; the feature GUC off; a loop of
   20; a non-superuser caller where there is a SQL surface; lolor and
   snowflake tables if present; session state afterwards. After each
   scenario: `diff all`, and grep every node's log for
   ERROR|TRAP|PANIC|FATAL|terminated by signal.
4. Everything you create, drop. Leave the cluster as you found it, and
   confirm with `service status` and `spock sub-show-status` at the end.
5. Write the report to {report_path}: verdict first, then a table of
   scenario | result | nodes, then any finding with cause, impact, a
   minimal repro and a log excerpt, then coverage gaps. Node names only:
   no hostnames, IP addresses, usernames or credentials anywhere.

Finish your reply with exactly one line of JSON and nothing after it:
{{"verdict": "PASS" or "FAIL" or "INCONCLUSIVE", "summary": "<one sentence>",
 "findings": ["<one line each>"], "scenarios": <number run>,
 "cluster": "{plan.cluster_name}", "nodes": ["<every node a scenario ran on>"]}}
The cluster and nodes fields are checked against the deployed cluster; a
verdict that names another cluster, or nodes it does not have, is rejected.
PASS means every scenario behaved and nothing in the logs is new. FAIL means
the change misbehaved or broke something adjacent. INCONCLUSIVE means you
could not reach the behaviour the PR is about; say why in the summary.
"""


def _counts(result):
    counts = result.get("tests") or {}
    if counts.get("total"):
        return (f"{counts.get('passed', 0)}/{counts['total']} passed"
                + (f", {counts['failed']} failed" if counts.get("failed") else ""))
    return result.get("status", "")


#: What Claude may run. Prefix-matched by Claude Code, so each entry is one
#: sub-command of a script: reading the cluster, driving a scenario, a
#: switchover or a stop/start through Patroni, the diff. Not here, and so
#: refused: node add/remove/ssh, spock rebuild, package, db create/drop,
#: service on etcd, pg_deploy_cluster.sh except --health_check, and any
#: other shell command — no ssh, pg_ctl, systemctl, docker, initdb, make.
ALLOWED_TOOLS = (
    "Bash(./pg_cluster_ctl.sh node list:*)",
    "Bash(./pg_cluster_ctl.sh node command:*)",
    "Bash(./pg_cluster_ctl.sh spock sub-show-status:*)",
    "Bash(./pg_cluster_ctl.sh spock sub-list:*)",
    "Bash(./pg_cluster_ctl.sh spock sub-disable:*)",
    "Bash(./pg_cluster_ctl.sh spock sub-enable:*)",
    "Bash(./pg_cluster_ctl.sh spock sub-wait-for-sync:*)",
    "Bash(./pg_cluster_ctl.sh spock sub-show-table:*)",
    "Bash(./pg_cluster_ctl.sh spock node-list:*)",
    "Bash(./pg_cluster_ctl.sh spock repset-list:*)",
    "Bash(./pg_cluster_ctl.sh spock repset-list-tables:*)",
    "Bash(./pg_cluster_ctl.sh spock replicate-ddl:*)",
    "Bash(./pg_cluster_ctl.sh spock no-primary-key:*)",
    "Bash(./pg_cluster_ctl.sh spock lag:*)",
    "Bash(./pg_cluster_ctl.sh spock version:*)",
    "Bash(./pg_cluster_ctl.sh db guc-show:*)",
    "Bash(./pg_cluster_ctl.sh db list:*)",
    "Bash(./pg_cluster_ctl.sh service status:*)",
    "Bash(./pg_cluster_ctl.sh service logs:*)",
    "Bash(./pg_cluster_ctl.sh service switchover:*)",
    "Bash(./pg_cluster_ctl.sh service stop:*)",
    "Bash(./pg_cluster_ctl.sh service start:*)",
    "Bash(./pg_cluster_ctl.sh diff:*)",
    "Bash(./pg_cluster_ctl.sh app:*)",
    "Bash(./pg_cluster_status.sh:*)",
    "Bash(./pg_deploy_cluster.sh --health_check:*)",
    "Bash(gh api:*)", "Bash(gh pr:*)", "Bash(curl:*)", "Bash(git:*)",
    "Bash(cat:*)", "Bash(grep:*)", "Bash(ls:*)",
    "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "Skill",
)


def run_claude(prompt, cwd, log_dir, command=None, timeout=None,
               progress=None):
    """Run Claude Code headless on this machine. Returns (code, text, stderr),
    where text is Claude's final reply.

    Output is streamed as JSON events so that every tool call shows on the
    console as it happens — a review takes long, and an hour of silence is
    indistinguishable from a hang. The raw stream is kept beside the log.

    Permissions are granted for what the prompt needs and nothing wider:
    sub-commands of the two scripts, gh/curl for GitHub, git for the Spock
    source, and files. Claude cannot ssh to the hosts itself or run a shell
    command outside that list; it goes through the scripts, which go through
    the saved state and the executor.
    """
    argv = [
        command or CLAUDE_COMMAND, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", *ALLOWED_TOOLS,
    ]
    log_dir = Path(log_dir)
    say = progress or (lambda line: None)
    final, last_text, is_error = None, "", False
    deadline = time.monotonic() + (timeout or CLAUDE_TIMEOUT)

    with open(log_dir / "claude-stream.jsonl", "w", encoding="utf-8") as raw, \
            open(log_dir / "claude-stderr.log", "w", encoding="utf-8") as err:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=err, text=True)
        try:
            for line in proc.stdout:
                raw.write(line)
                raw.flush()
                if time.monotonic() > deadline:
                    proc.kill()
                    say(f"    stopped after {timeout or CLAUDE_TIMEOUT}s "
                        f"(CLAUDE_PR_TIMEOUT)")
                    break
                event = _event(line)
                if event is None:
                    continue
                kind = event.get("type")
                if kind == "assistant":
                    for item in (event.get("message") or {}).get("content") or []:
                        if item.get("type") == "tool_use":
                            say(f"    > {_describe_tool(item)}")
                        elif item.get("type") == "text" and item.get("text"):
                            last_text = item["text"]
                elif kind == "result" or "result" in event and "duration_ms" in event:
                    final = event.get("result")
                    is_error = bool(event.get("is_error"))
                    if event.get("total_cost_usd") is not None:
                        say(f"    finished: {event.get('num_turns', '?')} turn(s), "
                            f"${event['total_cost_usd']:.2f}")
        finally:
            proc.stdout.close()
            code = proc.wait()

    text = final if isinstance(final, str) and final.strip() else last_text
    (log_dir / "claude-stdout.log").write_text(text or "", encoding="utf-8")
    stderr = (log_dir / "claude-stderr.log").read_text(encoding="utf-8")
    if is_error and not stderr.strip():
        stderr = text
    return code, text, stderr


def _event(line):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def _describe_tool(item):
    """One console line for a tool call: the command for Bash, the path
    for a file tool, the name otherwise."""
    name = item.get("name") or "?"
    inputs = item.get("input") or {}
    if name == "Bash":
        return (inputs.get("command") or "").strip().replace("\n", " ")[:160]
    for key in ("file_path", "path", "pattern", "url", "skill"):
        if inputs.get(key):
            return f"{name} {inputs[key]}"[:160]
    return name


def parse_claude_verdict(stdout):
    """The JSON line Claude was told to end with, or None."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip().strip("`")
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if "verdict" in data:
            data["verdict"] = str(data["verdict"]).upper()
            data.setdefault("findings", [])
            data.setdefault("summary", "")
            data.setdefault("cluster", "")
            data.setdefault("nodes", [])
            return data
    return None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _versions(cluster_name, db_password):
    rows = spock_rebuild.versions(cluster_name, db_password=db_password)
    return {r["node"]: r for r in rows}


def ran_on_this_cluster(claude, plan):
    """Why the verdict is not about the deployed cluster, or ''.

    The prompt asks for the cluster name and the nodes the scenarios ran
    on; a verdict that names another cluster, nodes this one does not have,
    or no node at all did not test what was asked.
    """
    if not claude:
        return ""
    named = str(claude.get("cluster") or "")
    if named != plan.cluster_name:
        return (f"the review says it ran on cluster {named or '(unnamed)'!s}, "
                f"not {plan.cluster_name}")
    nodes = claude.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        return "the review names no node a scenario ran on"
    known = {n.name for n in plan.nodes}
    foreign = sorted(set(map(str, nodes)) - known)
    if foreign:
        return (f"the review names node(s) this cluster does not have: "
                f"{', '.join(foreign)}")
    return ""


def decide(suites, claude, plan=None):
    """ALL GOOD only when both halves are good; otherwise what failed."""
    problems = []
    if plan is not None and claude is not None:
        elsewhere = ran_on_this_cluster(claude, plan)
        if elsewhere:
            problems.append(f"review not accepted: {elsewhere}")
            claude = dict(claude, verdict="INCONCLUSIVE", findings=[])
    for r in (suites or {}).get("results", []):
        if r.get("status") == "failed":
            problems.append(f"suite {r['component']} failed: "
                            f"{r.get('detail') or _counts(r)}")
    ran = [r for r in (suites or {}).get("results", [])
           if r.get("status") != "skipped"]

    if claude is None:
        problems.append("Claude's review did not produce a verdict")
    elif claude["verdict"] == "FAIL":
        problems.append(f"review: {claude['summary']}")
        problems.extend(f"finding: {f}" for f in claude["findings"])
    elif claude["verdict"] != "PASS" and not any(
            p.startswith("review not accepted") for p in problems):
        problems.append(f"review inconclusive: {claude['summary']}")

    if problems:
        verdict = FAIL if (claude and claude["verdict"] == "FAIL") or \
            any(p.startswith("suite ") for p in problems) else INCONCLUSIVE
        return verdict, problems
    if not ran:
        return INCONCLUSIVE, ["no suite ran against the cluster"]
    return ALL_GOOD, []


def run(cluster_name, components, change, db_password=None, node_name=None,
        inventory_path=None, tests=None, test_timeout=None,
        install_requirements=False, review=True, lookup=github_pull_request,
        claude=run_claude, run_logger=None):
    """Suites, then Claude's review. Returns a result dict, never raises.

    `lookup` and `claude` are how GitHub and Claude Code are reached; tests
    pass their own.
    """
    if isinstance(change, str):
        change = parse_change(change)
    started = time.time()
    log = run_logger or RunLogger(new_run_id(f"{cluster_name}-{change.slug}"))
    result = {
        "outcome": "failed", "verdict": INCONCLUSIVE, "problems": [],
        "cluster": cluster_name, "change": change.ref, "label": change.label,
        "url": change.url, "pull_request": None, "components": list(components),
        "versions": {}, "suites": None, "review": None, "warnings": [],
        "log_dir": str(log.root), "report": "", "claude_report": "",
    }

    try:
        plan, _ = state.load(cluster_name, db_password=db_password,
                             inventory_path=inventory_path)
        plan.db_password = state.resolve_password(plan, explicit=db_password)
        result["cluster"] = plan.cluster_name
        log.banner(f"Validating {change.label} on cluster {plan.cluster_name}")

        # --- the change ---------------------------------------------------
        log.step_start("Read the change", change.url)
        pr = lookup(change.number) if change.is_pull_request else None
        result["pull_request"] = pr
        if change.is_pull_request and pr is None:
            result["warnings"].append(
                "GitHub did not answer, so the PR's base branch is unknown "
                "and the cluster's Spock major was not checked against it")
            log.warn("    GitHub did not answer; base branch unknown")
        elif pr:
            log.info(f"    {pr['title']} — base {pr['base']}, head "
                     f"{pr['head_sha']}, {pr['state']}")
            problem = major_mismatch(plan, pr["base"])
            if problem:
                log.step_end("failed", "Spock major does not match")
                raise ValidationError(problem)
        log.step_end("passed", change.ref)

        # --- the cluster as found ------------------------------------------
        log.step_start("Record what every node runs", "spock version")
        versions = _versions(plan.cluster_name, db_password)
        result["versions"] = versions
        loaded = sorted({v.get("loaded") or "?" for v in versions.values()})
        for name, v in versions.items():
            log.info(f"    {name}: loaded {v['loaded']}, extension "
                     f"{v['extension']}, module {v['checksum']}")
        if len(loaded) > 1:
            log.step_end("failed", "nodes disagree")
            raise ValidationError(
                f"nodes load different Spock builds ({', '.join(loaded)}); "
                f"a verdict about the change needs every node on it")
        log.step_end("passed", f"every node loads {loaded[0]}")

        # --- suites --------------------------------------------------------
        log.step_start("Component suites", ", ".join(components))
        suites = health_check.run(
            plan.cluster_name, components, db_password=db_password,
            node_name=node_name, keep_going=True,
            install_requirements=install_requirements, tests=tests,
            test_timeout=test_timeout, inventory_path=inventory_path,
            run_logger=RunLogger(new_run_id(
                f"{plan.cluster_name}-{change.slug}-suites")))
        suites.pop("plan", None)
        result["suites"] = suites
        log.step_end("passed" if suites["outcome"] != "failed" else "failed",
                     f"{suites['outcome']} — {suites['log_dir']}")

        # --- Claude --------------------------------------------------------
        verdict = None
        if review:
            log.step_start("Claude reviews and tries to break the change",
                           f"up to {CLAUDE_TIMEOUT}s")
            if claude is run_claude and not claude_available():
                log.step_end("failed", f"{CLAUDE_COMMAND} not found")
                raise ValidationError(
                    f"Claude Code is not on this machine's PATH as "
                    f"{CLAUDE_COMMAND!r}; install it, or point CLAUDE_CMD "
                    f"at it. The suites above still ran")
            report_path = log.root / "claude-report.md"
            prompt = review_prompt(change, pr, plan, versions, suites,
                                   report_path, inventory_path=inventory_path)
            (log.root / "claude-prompt.md").write_text(prompt, encoding="utf-8")
            log.info(f"    {CLAUDE_COMMAND} -p ... (every tool call is "
                     f"shown; raw stream in {log.root}/claude-stream.jsonl)")
            code, stdout, stderr = claude(prompt, REPO_ROOT, log.root,
                                          progress=log.info)
            verdict = parse_claude_verdict(stdout)
            result["review"] = verdict
            result["claude_report"] = (str(report_path)
                                       if report_path.exists() else "")
            if verdict is None:
                tail = (stderr or stdout or "").strip().splitlines()[-3:]
                result["warnings"].append(
                    f"Claude exited {code} without a verdict line; see "
                    f"{log.root}/claude-stdout.log"
                    + (f" — {' | '.join(tail)}" if tail else ""))
                log.step_end("failed", f"exit {code}, no verdict")
            else:
                log.info(f"    {verdict['verdict']}: {verdict['summary']}")
                for finding in verdict["findings"]:
                    log.warn(f"    finding: {finding}")
                log.step_end("passed" if verdict["verdict"] == "PASS"
                             else "failed", verdict["summary"])
        else:
            result["warnings"].append("review skipped (--no-review): only "
                                      "the suites judged the change")
            verdict = {"verdict": "PASS", "summary": "not reviewed",
                       "findings": [], "scenarios": 0,
                       "cluster": plan.cluster_name,
                       "nodes": [n.name for n in plan.nodes]}

        result["verdict"], result["problems"] = decide(suites, verdict, plan)
        result["outcome"] = "succeeded"

    except Exception as exc:                        # noqa: BLE001 - reported
        result["outcome"] = "failed"
        result["error"] = str(exc)
        result["problems"] = [str(exc)]
        log.error(f"Validation stopped — {exc}")

    result["duration"] = round(time.time() - started, 1)
    result["report"] = write_report(result, log.root)
    log.banner(result["verdict"])
    for line in summary_lines(result):
        log.info(line)
    return result


# ---------------------------------------------------------------------------
# Telling the user
# ---------------------------------------------------------------------------


def summary_lines(result):
    out = [""]
    suites = result.get("suites") or {}
    rows = suites.get("results") or []
    if rows:
        headers = ("COMPONENT", "RESULT", "DETAIL")
        table = [(r["component"], r["status"].upper(),
                  r.get("detail") or _counts(r)) for r in rows]
        widths = [max(len(str(row[i])) for row in (headers, *table))
                  for i in range(len(headers))]

        def line(cells):
            return "  ".join(f"{str(c):<{widths[i]}}"
                             for i, c in enumerate(cells)).rstrip()
        out += [line(headers), "  ".join("-" * w for w in widths)]
        out += [line(row) for row in table]
        out.append("")

    review = result.get("review")
    if review:
        out.append(f"  Review: {review['verdict']} — {review['summary']}"
                   + (f" ({review.get('scenarios')} scenario(s))"
                      if review.get("scenarios") else ""))
        for finding in review.get("findings") or []:
            out.append(f"    - {finding}")
    for problem in result.get("problems") or []:
        out.append(f"  PROBLEM: {problem}")
    for warning in result.get("warnings") or []:
        out.append(f"  WARNING: {warning}")
    if result.get("claude_report"):
        out.append(f"  Claude's report: {result['claude_report']}")
    if result.get("report"):
        out.append(f"  Report: {result['report']}")
    return out


def write_report(result, directory):
    """A markdown report beside the log. Node names only."""
    pr = result.get("pull_request") or {}
    lines = [
        f"# {result['label']} on cluster {result['cluster']}",
        "",
        f"**{result['verdict']}**",
        "",
        f"- Change: {result['url']}",
    ]
    if pr:
        lines.append(f"- Title: {pr.get('title', '')}")
        lines.append(f"- Base branch: {pr.get('base', '')}; head "
                     f"{pr.get('head_sha', '')}; {pr.get('state', '')}"
                     + (", merged" if pr.get("merged") else ""))
    loaded = sorted({v.get("loaded") or "?" for v in
                     (result.get("versions") or {}).values()})
    if loaded:
        lines.append(f"- Spock loaded on every node: {', '.join(loaded)}")
    lines.append(f"- Components: {', '.join(result.get('components') or [])}")
    lines.append(f"- Run: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
                 f", {result.get('duration', 0)}s")
    lines.append("")

    rows = (result.get("suites") or {}).get("results") or []
    if rows:
        lines += ["## Suites", "", "| component | result | detail |",
                  "|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['component']} | {r['status']} | "
                         f"{r.get('detail') or _counts(r)} |")
        lines.append("")

    review = result.get("review")
    if review:
        lines += ["## Review", "", f"**{review['verdict']}** — "
                  f"{review['summary']}", ""]
        for finding in review.get("findings") or []:
            lines.append(f"- {finding}")
        if result.get("claude_report"):
            lines.append(f"\nFull review: `{result['claude_report']}`")
        lines.append("")

    for problem in result.get("problems") or []:
        lines.append(f"- Problem: {problem}")
    for warning in result.get("warnings") or []:
        lines.append(f"- Warning: {warning}")

    path = Path(directory) / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)
