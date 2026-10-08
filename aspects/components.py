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

from aspects import inventory, pg_extensions, platform_detect, source_build

# Where a packaged build puts things that are not tied to a PostgreSQL prefix.
PACKAGE_BIN = "/usr/bin"

# Said instead of repeating the prefix on every row that lives in it — which
# before the hosts are probed is two paths, once per family.
IN_PREFIX = "the PostgreSQL prefix above"


def _family_of(plan):
    """The platform, if the hosts have been probed. Empty before that."""
    families = {host.family for host in plan.hosts if host.family}
    if len(families) == 1:
        return families.pop()
    return ""


def prefix_of(plan, family=""):
    """Where PostgreSQL lives: a build prefix, or the packaged server's."""
    if plan.deploy_mode == "source":
        return source_build.install_dir(plan.pg_major)
    if family:
        return platform_detect.pg_bin_dir(family, plan.pg_major).rsplit("/", 1)[0]
    # Before the hosts are probed, both are still possible and saying so beats
    # guessing: the two families disagree about this path.
    return (f"/usr/pgsql-{plan.pg_major} (RHEL) | "
            f"/usr/lib/postgresql/{plan.pg_major} (Debian)")


def rows(plan):
    """One dict per component: {name, version, origin, location}."""
    family = _family_of(plan)
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
            "origin": source_build.SPOCK_REPO,
            "location": IN_PREFIX,
        })
    else:
        pinned = pins.get("spock", "")
        packages = platform_detect.server_packages(
            family or "rhel", plan.pg_major, plan.spock_major)
        listing.append({
            "name": f"spock{plan.spock_major}",
            "version": f"{pinned} (expected)" if pinned else "channel decides",
            "origin": f"{channel} — {packages[0]}" if family
                      else f"{channel} — the pgEdge spock{plan.spock_major} package",
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
            "origin": f"{channel} — {platform_detect.patroni_packages(family)[0]}"
                      if family else f"{channel} — the pgEdge Patroni package",
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
    if family == "rhel":
        return f"pgedge-postgresql{pg_major} (pulled in by the spock package)"
    if family == "deb":
        return f"pgedge-postgresql-{pg_major}"
    return "the pgEdge PostgreSQL package"


def _package_names(spec, family, pg_major):
    """How to name this package when the family is not yet known.

    A version-independent package is one name on both; an extension is two, and
    printing them as a pair is the only honest answer before the probe.
    """
    if family:
        return spec.package(family, pg_major)
    rhel = spec.package("rhel", pg_major)
    deb = spec.package("deb", pg_major)
    if rhel == deb:
        return rhel
    return f"{rhel} (RHEL) | {deb} (Debian)"


def extension_rows(plan, family, prefix, channel):
    """One row per optional add-on, which are not all the same shape."""
    listing = []
    for item in pg_extensions.selections_of(plan):
        spec = pg_extensions.CATALOG[item["name"]]
        from_source = item["mode"] == pg_extensions.MODE_SOURCE

        if from_source:
            version = f"{item.get('ref') or spec.default_ref} (branch or tag)"
            origin = spec.repo
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


def summary_lines(plan):
    """The component table, as lines for the plan summary."""
    listing = rows(plan)
    if not listing:
        return []

    name_width = max(len(row["name"]) for row in listing)
    version_width = max(len(row["version"]) for row in listing)
    lines = [
        f"{'COMPONENT':<{name_width}}  {'VERSION':<{version_width}}  "
        f"ORIGIN / INSTALLS TO"
    ]
    for row in listing:
        lines.append(
            f"{row['name']:<{name_width}}  {row['version']:<{version_width}}  "
            f"{row['origin']}"
        )
        lines.append(
            f"{'':<{name_width}}  {'':<{version_width}}  -> {row['location']}"
        )

    if builds_from_source(plan):
        lines.append("")
        lines.append(
            f"Sources are cloned and compiled under {source_build.BUILD_ROOT}; "
            f"only the results are installed to the paths above."
        )
    return lines


def builds_from_source(plan):
    """Is anything here compiled on the hosts rather than unpacked?"""
    if plan.deploy_mode == "source":
        return True
    return any(item["mode"] == pg_extensions.MODE_SOURCE
               for item in pg_extensions.selections_of(plan))
