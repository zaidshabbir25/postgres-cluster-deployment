#!/usr/bin/env python3
"""What the deployment will install, where each piece comes from, and where it
lands.

The plan summary used to name PostgreSQL and Spock and stop there, which left
the two questions people actually ask unanswered: which version is this going
to be, and where will it end up. Neither has a single answer — both depend on
whether the cluster is built from packages or from source, and for packages the
honest answer about a version is "whatever the channel is carrying today".

So each row says where its version came from:

  * `expected`   — a pin in configuration/config<major>.env. A reference point,
    not a gate: the deployment installs what the channel offers and records the
    result. Comparing the two afterwards is the point of the pin.
  * `exact`      — asked for explicitly, and the deployment fails if it cannot
    be had.
  * `branch`/`tag` — a git ref, for anything built from source.
  * `channel`    — not knowable before the package manager runs.
"""

from aspects import (inventory, pg_extensions, pg_server_management,
                     platform_detect, source_build)

# Where a packaged build puts things that are not tied to a PostgreSQL prefix.
PACKAGE_BIN = "/usr/bin"

# Said instead of repeating the prefix on every row that lives in it — which
# before the hosts are probed is two paths, once per family.
IN_PREFIX = "the PostgreSQL prefix"


ASSUMED_FAMILY = "rhel"


def _family_of(plan):
    """The platform, if the hosts have been probed. Empty before that."""
    families = {host.family for host in plan.hosts if host.family}
    if len(families) == 1:
        return families.pop()
    return ""


def shown_family(plan):
    """(family to render, whether that is an assumption).

    Before the hosts are probed either family is still possible, and the two
    disagree about every path and half the package names. Printing both in
    every cell turns the table into a wall; printing one and saying so above it
    keeps the table a table. The assumption is stated, never hidden.
    """
    family = _family_of(plan)
    if family:
        return family, False
    return ASSUMED_FAMILY, True


def built_major(plan):
    """The major a source build will actually use.

    Not plan.pg_major: build_major() takes it from the exact version it is
    given, so `--pg-version 18.6` builds pg18 even when --pg-major still says
    17. The prefix has to agree with that or the summary points at a directory
    the deployment never creates.
    """
    spec = plan.source_build or {}
    version = spec.get("pg_version") or plan.pg_version
    return pg_server_management.major_of(version) if version else plan.pg_major


def prefix_of(plan, family=""):
    """Where PostgreSQL lives: a build prefix, or the packaged server's."""
    if plan.deploy_mode == "source":
        return source_build.install_dir(built_major(plan))
    return platform_detect.pg_bin_dir(family or ASSUMED_FAMILY,
                                      plan.pg_major).rsplit("/", 1)[0]


def _short(url):
    """A git remote without the scheme — the column is long enough already."""
    return str(url).replace("https://", "").replace(".git", "")


def rows(plan):
    """One dict per component: {name, version, origin, location}."""
    family, _assumed = shown_family(plan)
    prefix = prefix_of(plan, family)
    source = plan.deploy_mode == "source"
    spec = plan.source_build or {}
    pins = inventory.expected_versions(plan.pg_major, plan.spock_major)
    channel = f"{plan.repo_channel} channel"

    listing = []

    # --- PostgreSQL ---------------------------------------------------
    if source:
        version = spec.get("pg_version") or plan.pg_version or plan.pg_major
        listing.append({
            "name": "PostgreSQL",
            "version": f"{version} (exact)",
            "origin": "ftp.postgresql.org source tarball",
            "location": prefix,
        })
    else:
        asked = plan.pg_version
        pinned = pins.get("pg_version", "")
        if asked:
            version = f"{asked} (exact)"
        elif pinned:
            version = f"{pinned} (expected)"
        else:
            version = f"{plan.pg_major}.x (channel decides)"
        listing.append({
            "name": "PostgreSQL",
            "version": version,
            "origin": f"{channel} — {_server_package(family, plan.pg_major)}",
            "location": prefix,
        })

    # --- Spock --------------------------------------------------------
    if source:
        branch = spec.get("spock_branch") or source_build.default_spock_branch(
            plan.spock_major)
        listing.append({
            "name": f"spock{plan.spock_major}",
            "version": f"{branch} (branch or tag)",
            "origin": _short(source_build.SPOCK_REPO),
            "location": IN_PREFIX,
        })
    else:
        pinned = pins.get("spock", "")
        packages = platform_detect.server_packages(
            family or "rhel", plan.pg_major, plan.spock_major)
        listing.append({
            "name": f"spock{plan.spock_major}",
            "version": f"{pinned} (expected)" if pinned else "channel decides",
            "origin": f"{channel} — {packages[0]}",
            "location": IN_PREFIX,
        })

    # --- the HA stack -------------------------------------------------
    if source:
        listing.append({
            "name": "Patroni",
            "version": "latest on PyPI",
            "origin": "pip",
            "location": source_build.PATRONI_VENV,
        })
        listing.append({
            "name": "etcd",
            "version": spec.get("etcd_version") or source_build.DEFAULT_ETCD_VERSION,
            "origin": "github.com/etcd-io/etcd release",
            "location": f"{PACKAGE_BIN}/etcd",
        })
    else:
        listing.append({
            "name": "Patroni",
            "version": f"{pins.get('patroni', '')} (expected)"
                       if pins.get("patroni") else "channel decides",
            "origin": f"{channel} — {platform_detect.patroni_packages(family)[0]}",
            "location": PACKAGE_BIN,
        })
        listing.append({
            "name": "etcd",
            "version": f"{pins.get('etcd', '')} (expected)"
                       if pins.get("etcd") else "channel decides",
            "origin": f"{channel} — pgedge-etcd",
            "location": f"{PACKAGE_BIN}/etcd",
        })

    listing += extension_rows(plan, family, prefix, channel)
    return listing


def _server_package(family, pg_major):
    """The package that provides the server itself.

    Not simply the first entry of server_packages(): that list leads with Spock
    deliberately, because installing Spock is what drags in the matching server
    and guarantees the two are ABI compatible. So on RHEL the server is never
    named on the command line at all, and saying which package it comes from
    means saying that.
    """
    if (family or ASSUMED_FAMILY) == "rhel":
        return f"pgedge-postgresql{pg_major}"
    return f"pgedge-postgresql-{pg_major}"


def _package_names(spec, family, pg_major):
    """The package name for the family being rendered."""
    return spec.package(family or ASSUMED_FAMILY, pg_major)


def extension_rows(plan, family, prefix, channel):
    """One row per optional add-on, which are not all the same shape."""
    listing = []
    for item in pg_extensions.selections_of(plan):
        spec = pg_extensions.CATALOG[item["name"]]
        from_source = item["mode"] == pg_extensions.MODE_SOURCE

        if from_source:
            version = f"{item.get('ref') or spec.default_ref} (branch or tag)"
            origin = _short(spec.repo)
        else:
            version = "channel decides"
            origin = f"{channel} — {_package_names(spec, family, plan.pg_major)}"

        if spec.is_tool:
            # Independent of PostgreSQL, so it goes where binaries go — and a
            # built one goes somewhere different from a packaged one.
            location = (f"{pg_extensions.TOOL_BIN_DIR}/{spec.binary or spec.name}"
                        if from_source else f"{PACKAGE_BIN}/{spec.binary or spec.name}")
            if from_source:
                location += f" (Go toolchain in {pg_extensions.GO_ROOT})"
        else:
            location = IN_PREFIX

        listing.append({"name": spec.name, "version": version,
                        "origin": origin, "location": location})
    return listing


# Past this, a cell stops helping and starts pushing the table off the screen.
# Wide enough for the longest real package name — Debian's
# pgedge-postgresql-17-snowflake — so the common case stays in the table.
MAX_CELL = 48


def summary_lines(plan):
    """The component table, as lines for the plan summary.

    Four columns rather than a wrapped sentence per row, so the three things
    people scan for — what version, from where, to where — line up down the
    page. Anything too long for a cell becomes a numbered note underneath
    instead of stretching the table past a terminal's width; a table nobody can
    read without wrapping has given up the only advantage it had.
    """
    listing = rows(plan)
    if not listing:
        return []

    family, assumed = shown_family(plan)
    prefix = prefix_of(plan, family)
    notes = []

    def note(text):
        """Park an over-long value below the table and return its marker."""
        if text not in notes:
            notes.append(text)
        return f"[{notes.index(text) + 1}]"

    def fit(value, keep=""):
        """Shorten a cell to a stub plus a note, when it is too long.

        `keep` is the part worth seeing in the column itself — the path, say,
        with the parenthetical moved out of the way.
        """
        if len(value) <= MAX_CELL:
            return value
        head, _, tail = value.partition(" (")
        if tail and len(head) <= MAX_CELL - 4:
            return f"{head} {note(tail.rstrip(')'))}"
        head, _, tail = value.partition(" — ")
        if tail and len(head) <= MAX_CELL - 4:
            return f"{head} {note(tail)}"
        return f"{keep or value[:MAX_CELL - 4].rstrip()} {note(value)}"

    table = []
    for row in listing:
        location = prefix if row["location"] == IN_PREFIX else row["location"]
        table.append((row["name"], row["version"], fit(row["origin"]),
                      fit(location)))

    lines = _render_table(("COMPONENT", "VERSION", "SOURCE", "INSTALLS TO"),
                          table)
    if assumed and _family_matters(plan):
        lines.insert(0, f"  Paths and package names below are the "
                        f"{ASSUMED_FAMILY.upper()} ones; the hosts have not "
                        f"been probed yet, and Debian spells both differently.")
        lines.insert(1, "")

    if notes:
        lines.append("")
        lines += [f"  [{index + 1}] {text}" for index, text in enumerate(notes)]
    if builds_from_source(plan):
        lines.append("")
        lines.append(
            f"  Sources are cloned and compiled under {source_build.BUILD_ROOT}; "
            f"only the results are installed."
        )
    return lines


def _render_table(headers, table):
    """Aligned columns with a rule under the header, sized to the content."""
    widths = [
        max(len(str(row[index])) for row in (headers, *table))
        for index in range(len(headers))
    ]

    def line(cells):
        return "  ".join(
            f"{str(cell):<{widths[i]}}" for i, cell in enumerate(cells)
        ).rstrip()

    rendered = [line(headers)] + [line(row) for row in table]
    # The rule spans what is actually printed, not the padded column widths:
    # a rule running past the last value looks like a mistake.
    rule = "  ".join("-" * width for width in widths)
    return [rendered[0], rule[:max(len(text) for text in rendered)], *rendered[1:]]


def _family_matters(plan):
    """Would knowing the platform change anything in this table?

    Not for a build: it installs to /opt/pgedge whatever the distribution is,
    and names no packages. Saying "these are the RHEL paths" there would be a
    caveat about nothing.
    """
    if plan.deploy_mode != "source":
        return True
    return any(item["mode"] != pg_extensions.MODE_SOURCE
               for item in pg_extensions.selections_of(plan))


def builds_from_source(plan):
    """Is anything here compiled on the hosts rather than unpacked?"""
    if plan.deploy_mode == "source":
        return True
    return any(item["mode"] == pg_extensions.MODE_SOURCE
               for item in pg_extensions.selections_of(plan))
