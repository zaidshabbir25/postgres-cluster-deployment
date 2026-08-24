#!/usr/bin/env python3
"""Turn a deployment result into a durable report.

Every run writes reports/runs/<run_id>/ containing:

    report.html   self-contained page — no CDN, opens from a file:// URL
    report.json   the same data, for CI to assert on
    summary.txt   the terminal recap, so a scrolled-away run is recoverable

reports/index.html lists every run, newest first, so a history of deployments
accumulates without anyone maintaining it.

The HTML is generated with html.escape on every interpolated value: node names,
package versions and especially command output all come from remote hosts and
must not be able to inject markup into a page an operator opens.
"""

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_ROOT = REPO_ROOT / "reports" / "runs"
INDEX_PATH = REPO_ROOT / "reports" / "index.html"

STATUS_LABEL = {
    "passed": "PASS",
    "failed": "FAIL",
    "skipped": "SKIP",
    "running": "…",
    "ok": "OK",
    "degraded": "DEGRADED",
    "down": "DOWN",
    "unknown": "UNKNOWN",
    "succeeded": "SUCCEEDED",
}


def _e(value):
    """Escape a value for HTML text content."""
    return html.escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate(result, kind="deployment"):
    """Write the report set for one run. Returns the directory."""
    plan = result.get("plan")
    run_id = result.get("run_id") or (plan.run_id if plan else "run")
    directory = REPORT_ROOT / run_id
    directory.mkdir(parents=True, exist_ok=True)

    payload = _payload(result, kind)
    (directory / "report.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    (directory / "summary.txt").write_text(_summary_text(payload), encoding="utf-8")
    (directory / "report.html").write_text(_html(payload), encoding="utf-8")

    _write_index()
    return directory


def _payload(result, kind):
    plan = result.get("plan")
    return {
        "kind": kind,
        "run_id": result.get("run_id"),
        "cluster": result.get("cluster"),
        "outcome": result.get("outcome", "unknown"),
        "failure": result.get("failure"),
        "duration": result.get("duration"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": result.get("counts", {}),
        "steps": result.get("steps", []),
        "warnings": result.get("warnings", []),
        "log_dir": result.get("log_dir"),
        "plan": plan.to_dict() if plan is not None else result.get("plan_dict", {}),
        "summary_lines": plan.summary_lines() if plan is not None else [],
        "results": _serialisable(result.get("results", {})),
        "health": result.get("health") or result.get("results", {}).get("health"),
    }


def _serialisable(value):
    """Drop anything that will not survive json.dumps cleanly."""
    if isinstance(value, dict):
        return {k: _serialisable(v) for k, v in value.items() if k != "plan"}
    if isinstance(value, list):
        return [_serialisable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------------


def _summary_text(payload):
    counts = payload["counts"]
    lines = [
        f"{payload['kind'].title()} report — {payload['cluster']}",
        f"Run       : {payload['run_id']}",
        f"Outcome   : {payload['outcome']}",
        f"Duration  : {payload['duration']}s",
        f"Steps     : {counts.get('passed', 0)} passed, "
        f"{counts.get('failed', 0)} failed, {counts.get('skipped', 0)} skipped",
        "",
    ]
    if payload["failure"]:
        lines += [f"Failure   : {payload['failure']}", ""]

    lines += payload["summary_lines"]
    lines += ["", "Steps:"]
    for index, step in enumerate(payload["steps"], 1):
        label = STATUS_LABEL.get(step["status"], step["status"])
        lines.append(
            f"  {index:2d}. [{label:>4}] {step['name']}"
            + (f" ({step['duration']}s)" if step.get("duration") else "")
        )
        if step.get("message"):
            lines.append(f"        {step['message']}")

    if payload["warnings"]:
        lines += ["", "Warnings:"]
        lines += [f"  - {warning}" for warning in payload["warnings"]]

    health = payload.get("health")
    if health:
        lines += ["", f"Health: {health.get('status')}"]
        for problem in health.get("problems", []):
            lines.append(f"  - {problem}")

    lines += ["", f"Logs: {payload['log_dir']}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #1b1f24; --muted: #5b6672;
  --line: #dfe3e8; --accent: #1f6feb;
  --pass: #1a7f45; --fail: #b3261e; --skip: #8a6d1f; --warn: #9a5b00;
  --pass-bg: #e7f5ec; --fail-bg: #fdecea; --skip-bg: #fdf4dd; --warn-bg: #fff4e5;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1216; --panel: #171b21; --ink: #e6e9ee; --muted: #9aa4b1;
    --line: #2a313a; --accent: #6ea8ff;
    --pass: #6fd08c; --fail: #ff8b7e; --skip: #e2c05f; --warn: #ffb968;
    --pass-bg: #14281c; --fail-bg: #2c1714; --skip-bg: #2a2413; --warn-bg: #2b1f10;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .75rem; letter-spacing: .01em; }
.sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }
.panel {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem 1.15rem; margin-bottom: 1rem;
}
.grid { display: grid; gap: .75rem; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: .8rem 1rem; }
.stat .k { color: var(--muted); font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; }
.stat .v { font-size: 1.5rem; font-weight: 600; margin-top: .15rem; }
.badge {
  display: inline-block; padding: .1rem .5rem; border-radius: 999px;
  font-size: .72rem; font-weight: 700; letter-spacing: .04em;
}
.b-pass, .b-ok, .b-succeeded { color: var(--pass); background: var(--pass-bg); }
.b-fail, .b-down, .b-failed { color: var(--fail); background: var(--fail-bg); }
.b-skip, .b-unknown { color: var(--skip); background: var(--skip-bg); }
.b-degraded { color: var(--warn); background: var(--warn-bg); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .88rem; min-width: 640px; }
th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: .74rem; text-transform: uppercase; letter-spacing: .05em; }
tr:last-child td { border-bottom: 0; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .82rem; }
pre {
  background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: .7rem .85rem; overflow-x: auto; margin: .4rem 0 0;
}
ul { margin: .3rem 0 0; padding-left: 1.2rem; }
li { margin: .2rem 0; }
.step { border-left: 3px solid var(--line); padding-left: .8rem; margin-bottom: .7rem; }
.step.passed { border-color: var(--pass); }
.step.failed { border-color: var(--fail); }
.step.skipped { border-color: var(--skip); }
.step .t { font-weight: 600; }
.step .m { color: var(--muted); font-size: .85rem; }
.dim { color: var(--muted); }
footer { margin-top: 2.5rem; color: var(--muted); font-size: .8rem; }
"""


def _badge(status):
    label = STATUS_LABEL.get(status, str(status).upper())
    return f'<span class="badge b-{_e(status)}">{_e(label)}</span>'


def _html(payload):
    plan = payload.get("plan") or {}
    counts = payload["counts"]
    health = payload.get("health") or {}

    parts = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{_e(payload['cluster'])} — {_e(payload['kind'])} report</title>",
        f"<style>{CSS}</style></head><body><div class=\"wrap\">",
        f"<h1>{_e(payload['cluster'])} {_badge(payload['outcome'])}</h1>",
        f"<p class=\"sub\">{_e(payload['kind']).title()} run "
        f"<code>{_e(payload['run_id'])}</code> · "
        f"{_e(payload['generated_at'])} · {_e(payload['duration'])}s</p>",
    ]

    # --- stats --------------------------------------------------------
    stats = [
        ("Steps passed", counts.get("passed", 0)),
        ("Steps failed", counts.get("failed", 0)),
        ("Spock nodes", len([n for n in plan.get("nodes", []) if n.get("role") == "spock"])),
        ("Standbys", len([n for n in plan.get("nodes", []) if n.get("role") == "standby"])),
        ("Hosts", len(plan.get("hosts", []))),
    ]
    if health:
        stats.append(("Health", STATUS_LABEL.get(health.get("status"), "—")))
    parts.append("<div class=\"grid\">")
    for key, value in stats:
        parts.append(
            f"<div class=\"stat\"><div class=\"k\">{_e(key)}</div>"
            f"<div class=\"v\">{_e(value)}</div></div>"
        )
    parts.append("</div>")

    if payload.get("failure"):
        parts.append(
            f"<h2>Failure</h2><div class=\"panel\"><pre>{_e(payload['failure'])}</pre></div>"
        )

    # --- configuration ------------------------------------------------
    parts.append("<h2>Configuration</h2><div class=\"panel\"><div class=\"scroll\">")
    parts.append("<table><tbody>")
    for key, value in (
        ("Deployment mode", plan.get("deploy_mode")),
        ("Repository channel", plan.get("repo_channel")),
        ("PostgreSQL", f"{plan.get('pg_major')} ({plan.get('pg_version') or 'n/a'})"),
        ("Spock major", f"spock{plan.get('spock_major')}"),
        ("Database", f"{plan.get('db_name')} as {plan.get('db_user')}"),
        ("Data root", plan.get("data_root")),
        ("etcd endpoints", ", ".join(plan.get("etcd_endpoints", []))),
        ("zodan script", plan.get("zodan_sql") or "—"),
    ):
        parts.append(f"<tr><th>{_e(key)}</th><td>{_e(value)}</td></tr>")
    parts.append("</tbody></table></div></div>")

    # --- topology -----------------------------------------------------
    parts.append("<h2>Topology</h2><div class=\"panel\"><div class=\"scroll\"><table>")
    parts.append(
        "<thead><tr><th>Node</th><th>Role</th><th>Host</th><th>Address</th>"
        "<th>PG port</th><th>REST</th><th>Scope</th><th>Follows</th>"
        "<th>Standbys</th></tr></thead><tbody>"
    )
    for node in plan.get("nodes", []):
        parts.append(
            "<tr>"
            f"<td><code>{_e(node.get('name'))}</code></td>"
            f"<td>{_e(node.get('role'))}</td>"
            f"<td>{_e(node.get('host'))}</td>"
            f"<td><code>{_e(node.get('address'))}</code></td>"
            f"<td>{_e(node.get('pg_port'))}</td>"
            f"<td>{_e(node.get('restapi_port'))}</td>"
            f"<td>{_e(node.get('scope'))}</td>"
            f"<td>{_e(node.get('leader') or '—')}</td>"
            f"<td>{_e(', '.join(node.get('standbys') or []) or '—')}</td>"
            "</tr>"
        )
    parts.append("</tbody></table></div></div>")

    # --- steps --------------------------------------------------------
    parts.append("<h2>Steps</h2><div class=\"panel\">")
    for index, step in enumerate(payload["steps"], 1):
        parts.append(f"<div class=\"step {_e(step['status'])}\">")
        parts.append(
            f"<div class=\"t\">{index}. {_e(step['name'])} "
            f"{_badge(step['status'])} "
            f"<span class=\"dim\">{_e(step.get('duration') or 0)}s</span></div>"
        )
        if step.get("detail"):
            parts.append(f"<div class=\"m\">{_e(step['detail'])}</div>")
        if step.get("message"):
            parts.append(f"<div class=\"m\">{_e(step['message'])}</div>")
        parts.append("</div>")
    parts.append("</div>")

    # --- health -------------------------------------------------------
    if health:
        parts.append(f"<h2>Health {_badge(health.get('status', 'unknown'))}</h2>")
        parts.append("<div class=\"panel\"><div class=\"scroll\"><table>")
        parts.append(
            "<thead><tr><th>Node</th><th>Role</th><th>Patroni</th><th>State</th>"
            "<th>Accepts writes</th><th>Subscriptions</th><th>Status</th>"
            "</tr></thead><tbody>"
        )
        for node in health.get("nodes", []):
            subs = (
                f"{node.get('subscriptions_replicating', '—')}/"
                f"{node.get('subscriptions_expected', '—')}"
                if node.get("role") == "spock" else "—"
            )
            writes = {True: "yes", False: "no", None: "?"}.get(
                node.get("accepting_writes"), "?"
            )
            parts.append(
                "<tr>"
                f"<td><code>{_e(node.get('name'))}</code></td>"
                f"<td>{_e(node.get('role'))}</td>"
                f"<td>{_e(node.get('patroni_role') or '—')}</td>"
                f"<td>{_e(node.get('patroni_state') or '—')}</td>"
                f"<td>{_e(writes)}</td>"
                f"<td>{_e(subs)}</td>"
                f"<td>{_badge(node.get('status', 'unknown'))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table></div>")

        if health.get("problems"):
            parts.append("<p class=\"dim\">Problems detected:</p><ul>")
            for problem in health["problems"]:
                parts.append(f"<li>{_e(problem)}</li>")
            parts.append("</ul>")
        parts.append("</div>")

    # --- packages -----------------------------------------------------
    packages = payload.get("results", {}).get("packages") or {}
    if packages:
        parts.append("<h2>Installed pgEdge packages</h2><div class=\"panel\">")
        for host_name, entries in packages.items():
            parts.append(f"<p><strong>{_e(host_name)}</strong></p>")
            parts.append("<div class=\"scroll\"><table><thead><tr>"
                         "<th>Package</th><th>Version</th></tr></thead><tbody>")
            for entry in entries:
                parts.append(
                    f"<tr><td><code>{_e(entry.get('package'))}</code></td>"
                    f"<td>{_e(entry.get('version'))}</td></tr>"
                )
            parts.append("</tbody></table></div>")
        parts.append("</div>")

    # --- replication verification -------------------------------------
    replication = payload.get("results", {}).get("replication")
    if replication:
        parts.append("<h2>Replication verification</h2><div class=\"panel\">")
        parts.append("<div class=\"scroll\"><table><thead><tr>"
                     "<th>Node</th><th>Sees nodes</th><th>Replicating</th>"
                     "<th>Slots</th><th>Streaming peers</th>"
                     "</tr></thead><tbody>")
        for entry in replication.get("nodes", []):
            slots = ", ".join(
                f"{s['name']}({'active' if s['active'] else 'idle'})"
                for s in entry.get("slots", [])
            )
            streaming = ", ".join(
                f"{p['peer']}:{p['state']}" for p in entry.get("streaming", [])
            )
            parts.append(
                "<tr>"
                f"<td><code>{_e(entry.get('node'))}</code></td>"
                f"<td>{_e(', '.join(entry.get('nodes_visible') or []))}</td>"
                f"<td>{_e(entry.get('replicating'))}/"
                f"{_e(entry.get('expected_subscriptions'))}</td>"
                f"<td>{_e(slots or '—')}</td>"
                f"<td>{_e(streaming or '—')}</td>"
                "</tr>"
            )
        parts.append("</tbody></table></div>")
        if replication.get("problems"):
            parts.append("<ul>")
            for problem in replication["problems"]:
                parts.append(f"<li>{_e(problem)}</li>")
            parts.append("</ul>")
        parts.append("</div>")

    # --- warnings -----------------------------------------------------
    if payload["warnings"]:
        parts.append(f"<h2>Warnings ({len(payload['warnings'])})</h2>")
        parts.append("<div class=\"panel\"><ul>")
        for warning in payload["warnings"]:
            parts.append(f"<li>{_e(warning)}</li>")
        parts.append("</ul></div>")

    parts.append(
        f"<footer>Logs for this run: <code>{_e(payload['log_dir'])}</code></footer>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Index of all runs
# ---------------------------------------------------------------------------


def _write_index():
    """Rebuild reports/index.html from the report.json of every run."""
    runs = []
    if REPORT_ROOT.exists():
        for directory in sorted(REPORT_ROOT.iterdir(), reverse=True):
            report = directory / "report.json"
            if not report.exists():
                continue
            try:
                payload = json.loads(report.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            runs.append(
                {
                    "run_id": payload.get("run_id") or directory.name,
                    "cluster": payload.get("cluster"),
                    "kind": payload.get("kind"),
                    "outcome": payload.get("outcome"),
                    "generated_at": payload.get("generated_at"),
                    "duration": payload.get("duration"),
                    "path": f"runs/{directory.name}/report.html",
                }
            )

    rows = []
    for run in runs:
        rows.append(
            "<tr>"
            f"<td><a href=\"{_e(run['path'])}\"><code>{_e(run['run_id'])}</code></a></td>"
            f"<td>{_e(run['cluster'])}</td>"
            f"<td>{_e(run['kind'])}</td>"
            f"<td>{_badge(run['outcome'])}</td>"
            f"<td>{_e(run['duration'])}s</td>"
            f"<td class=\"dim\">{_e(run['generated_at'])}</td>"
            "</tr>"
        )

    body = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Cluster deployment reports</title>"
        f"<style>{CSS}</style></head><body><div class=\"wrap\">"
        "<h1>Cluster deployment reports</h1>"
        f"<p class=\"sub\">{len(runs)} run(s) recorded · newest first</p>"
        "<div class=\"panel\"><div class=\"scroll\"><table><thead><tr>"
        "<th>Run</th><th>Cluster</th><th>Kind</th><th>Outcome</th>"
        "<th>Duration</th><th>Generated</th></tr></thead><tbody>"
        + ("".join(rows) or "<tr><td colspan=\"6\" class=\"dim\">No runs yet.</td></tr>")
        + "</tbody></table></div></div></div></body></html>"
    )

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".html.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, INDEX_PATH)
    return INDEX_PATH
