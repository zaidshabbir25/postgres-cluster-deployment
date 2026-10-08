#!/usr/bin/env python3
"""The summary printed when a deployment finishes.

The plan summary answers "what is about to happen". This answers "what
happened" in the same shape, so the two can be read against each other: one row
per component, the version that actually landed rather than the one that was
expected, and whether it worked.

A failed row is worth more than a failed run: a deployment that got PostgreSQL
and Spock up but could not build ACE has one problem, not six, and the row that
failed names the log to read. Components a failure stopped it from reaching are
marked as such rather than as failures of their own — "not reached" and "broken"
lead somewhere different.
"""

import re

from aspects import components, pg_extensions

# Which step is responsible for each component. The first one that exists in
# the run decides the row's verdict; a component touched by several steps
# (installed, then created) fails if any of them did.
STEPS_FOR = {
    "PostgreSQL": ("Build PostgreSQL and Spock from source",
                   "Install PostgreSQL and Spock packages"),
    "spock": ("Build PostgreSQL and Spock from source",
              "Install PostgreSQL and Spock packages",
              "Create Spock extensions"),
    "Patroni": ("Build PostgreSQL and Spock from source",
                "Install Patroni and etcd packages",
                "Write Patroni configuration"),
    "etcd": ("Build PostgreSQL and Spock from source",
             "Install Patroni and etcd packages",
             "Configure and start etcd"),
    "extension": ("Install optional extensions",
                  "Create optional extensions"),
    "tool": ("Install optional extensions",),
}

CROSSWIRE_STEP = "Cross-wire Spock nodes"
STANDBY_STEP = "Add standby nodes"

OK = "OK"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
NOT_REACHED = "not reached"


def _step_index(result):
    """Steps by name, in the order they ran."""
    return {step["name"]: step for step in result.get("steps", [])}


def _verdict(steps, names):
    """The worst outcome among the steps responsible for one component.

    A step that never ran is not a failure of its own: the run stopped
    somewhere earlier, and saying so points at the real problem instead of
    spreading it across every row underneath.
    """
    seen = [steps[name] for name in names if name in steps]
    if not seen:
        return NOT_REACHED, ""
    for step in seen:
        if step["status"] == "failed":
            return FAILED, f"{step['name']}: {step.get('message', '')}".strip(": ")
    if all(step["status"] == "skipped" for step in seen):
        return SKIPPED, ""
    return OK, ""


def component_rows(result):
    """One row per component: name, version, source, verdict, detail."""
    plan = result.get("plan")
    if plan is None:
        return []

    steps = _step_index(result)
    results = result.get("results") or {}
    planned = {row["name"]: row for row in components.rows(plan)}
    rows = []

    for name, row in planned.items():
        discovered = True
        if name == "PostgreSQL":
            # --pg-version puts a number here before anything is built, so it
            # is an intention, not a result. Only one step installs PostgreSQL,
            # so that step's verdict is the whole answer.
            actual = plan.pg_version
            discovered = plan.deploy_mode != "source"
            verdict, detail = _verdict(steps, STEPS_FOR["PostgreSQL"])
        elif name.startswith("spock"):
            found = results.get("spock_versions") or {}
            actual = next((v for v in found.values() if v), "")
            verdict, detail = _verdict(steps, STEPS_FOR["spock"])
        elif name in ("Patroni", "etcd"):
            actual = _packaged_version(results, name)
            verdict, detail = _verdict(steps, STEPS_FOR[name])
        else:
            spec = pg_extensions.CATALOG.get(name)
            key = "tool" if spec is not None and spec.is_tool else "extension"
            actual = _addon_version(results, name, spec)
            verdict, detail = _verdict(steps, STEPS_FOR[key])

        # One step installs or creates several things, so its failure does not
        # belong to all of them. A component that reported its own version is
        # there and working whatever else went wrong in the same step; the row
        # for the one that actually broke still carries the failure.
        if actual and discovered and verdict == FAILED:
            verdict, detail = OK, ""

        rows.append({
            "name": name,
            "version": actual or _planned(row["version"]),
            "source": _origin_with_ref(row, plan, name),
            "status": verdict,
            "detail": detail,
        })
    return rows


def _planned(version):
    """A version nothing confirmed: say it was only ever the intention.

    The qualifiers the plan uses — (expected), (exact), (branch or tag) — are
    about where the number came from, which stops being the interesting part
    once the run is over. What matters here is that nothing reported back.
    """
    bare = str(version).split(" (")[0].strip()
    if not bare:
        return "-"
    # "latest on PyPI" is already a description of the intention; tacking
    # "(planned)" onto it says the same thing twice. A bare ref like "main" is
    # not — it reads as a fact until it is marked.
    if " " in bare:
        return bare
    return f"{bare} (planned)"


def _origin_with_ref(row, plan, name):
    """The source, with the ref folded in — one column instead of two."""
    origin = row["origin"]
    version = row["version"]
    if "(branch or tag)" in version:
        return f"{origin}@{version.split(' (')[0]}"
    return origin


# pgedge-patroni-etcd contains both words, so matching on a substring alone
# would report Patroni's version for etcd. Exact names first, substring after.
PACKAGE_NAMES = {
    "Patroni": ("pgedge-patroni", "pgedge-patroni-etcd"),
    "etcd": ("pgedge-etcd",),
}


def _packaged_version(results, name):
    """Find a component's version in the installed-package inventory."""
    installed = {}
    for entries in (results.get("packages") or {}).values():
        for entry in entries:
            if entry.get("package"):
                installed.setdefault(entry["package"], entry.get("version", ""))
    for candidate in PACKAGE_NAMES.get(name, ()):
        if installed.get(candidate):
            return installed[candidate]
    return ""


def _addon_version(results, name, spec):
    """What an optional add-on reported once it was installed."""
    if spec is not None and spec.is_tool:
        for host in (results.get("pg_extensions") or {}).values():
            version = (host.get("tools") or {}).get(name)
            if version:
                # A tool answers --version with its own name in front of the
                # number; the column already says which tool this is.
                return re.sub(rf"^{re.escape(name)}\s+(version\s+)?", "",
                              version.strip(), flags=re.IGNORECASE)
        return ""
    return (results.get("extension_versions") or {}).get(name, "")


# ---------------------------------------------------------------------------
# replication
# ---------------------------------------------------------------------------


def replication_rows(result):
    """Cross-wiring and the standbys, as (label, detail, status) rows."""
    plan = result.get("plan")
    if plan is None:
        return []

    steps = _step_index(result)
    rows = []

    spock_nodes = plan.spock_nodes
    expected = len(spock_nodes) * (len(spock_nodes) - 1)
    if len(spock_nodes) < 2:
        rows.append(("Cross-wiring",
                     "single node — nothing to cross-wire", SKIPPED))
    else:
        status, detail = _verdict(steps, (CROSSWIRE_STEP,))
        rows.append((
            "Cross-wiring",
            f"{len(spock_nodes)} Spock nodes, {expected} subscriptions "
            f"({', '.join(n.name for n in spock_nodes)})",
            status,
        ))
        if detail:
            rows.append(("", detail, ""))

    rows += standby_rows(result, steps)
    return rows


def standby_rows(result, steps=None):
    """One row per leader-standby pair, with how that scope replicates."""
    from aspects import patroni_management

    plan = result["plan"]
    steps = steps if steps is not None else _step_index(result)
    standbys = plan.standby_nodes
    if not standbys:
        return [("Standby nodes", "none requested", SKIPPED)]

    status, _ = _verdict(steps, (STANDBY_STEP,))
    rows = [("Standby nodes", f"{len(standbys)} across "
                              f"{len({s.scope for s in standbys})} scope(s)",
             status)]
    if status in (NOT_REACHED, SKIPPED):
        # Patroni was never asked to create these, so "not streaming" is not a
        # fault of theirs — it is the earlier failure, already reported.
        return rows + [(f"  {s.leader or '?'} -> {s.name}",
                        "not created", status) for s in standbys]

    members = _members_by_name(result)
    for standby in standbys:
        leader = standby.leader or "?"
        try:
            # Ask the settings, not the prose: "asynchronous" contains
            # "synchronous", so matching the description gets it backwards.
            settings = patroni_management.sync_settings(plan, plan.node(leader))
            mode = "synchronous" if settings else "asynchronous"
            if settings.get("synchronous_mode") == "quorum":
                mode = "quorum synchronous"
        except KeyError:
            mode = "unknown"
        member = members.get(standby.name, {})
        role = member.get("role") or ""
        state = member.get("state") or ""
        live = f"{role}/{state}".strip("/") or "not visible in Patroni"
        rows.append((
            f"  {leader} -> {standby.name}",
            f"{mode}, {live}",
            OK if state.lower() in ("running", "streaming") else FAILED,
        ))
    return rows


def _members_by_name(result):
    """Patroni's own view of every member, from the health snapshot."""
    snapshot = (result.get("results") or {}).get("health") or {}
    found = {}
    for scope in (snapshot.get("scopes") or {}).values():
        for member in scope.get("members", []):
            if member.get("name"):
                found[member["name"]] = member
    return found


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def lines(result):
    """The whole summary, as lines to print."""
    rows = component_rows(result)
    if not rows:
        return []

    notes = []
    table = []
    for row in rows:
        status = row["status"]
        if row["detail"]:
            if row["detail"] not in notes:
                notes.append(row["detail"])
            status = f"{status} [{notes.index(row['detail']) + 1}]"
        table.append((row["name"], row["version"], row["source"], status))

    out = ["PostgreSQL Cluster Setup Summary", ""]
    out += components._render_table(
        ("COMPONENT", "VERSION", "BUILT / INSTALLED FROM", "RESULT"), table
    )

    replication = replication_rows(result)
    if replication:
        out += ["", "Replication", ""]
        out += components._render_table(
            ("WHAT", "DETAIL", "RESULT"),
            [(label, detail, status) for label, detail, status in replication],
        )

    if notes:
        out.append("")
        for index, note in enumerate(notes, start=1):
            out.append(f"  [{index}] {note}")

    log_dir = result.get("log_dir")
    if log_dir and any(row["status"] == FAILED for row in rows):
        out.append("")
        out.append(f"  Full output for every step: {log_dir}/deploy.log")
        out.append(f"  Per-node output: {log_dir}/<node>.log")
    return out
