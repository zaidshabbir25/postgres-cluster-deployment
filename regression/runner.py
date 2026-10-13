#!/usr/bin/env python3
"""The contract every component's regression suite implements.

A suite is asked three questions in order, and each has a distinct answer worth
reporting:

  1. `missing_requirements` — can this run here at all? A host without Go
     cannot run ACE's tests, and saying "skipped: no Go toolchain" is a
     different fact from "failed".
  2. `prepare` — put the suite's source on the host, at the ref the component
     was built from. A v5.0.5 cluster is tested by v5.0.5's tests; a newer
     suite tests behaviour the installed extension does not have.
  3. `execute` — run it, and turn its output into counts.

Each component works inside its own directory under REGRESSION_ROOT, so a
rerun of one suite cannot disturb another's artefacts, and a failure leaves its
diffs where they can be read.
"""

import re
import shlex
import time
from dataclasses import dataclass, field

REGRESSION_ROOT = "/opt/pgedge/regression"

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class SuiteResult:
    """What one component's suite did."""

    component: str
    status: str = SKIPPED
    detail: str = ""
    tests: dict = field(default_factory=dict)   # total / passed / failed
    duration: float = 0.0
    workspace: str = ""
    artefacts: list = field(default_factory=list)
    targets_cluster: bool = True
    ref: str = ""
    output: str = ""

    @property
    def ok(self):
        return self.status in (PASSED, SKIPPED)

    def summary(self):
        counts = self.tests
        if counts.get("total"):
            return (f"{counts.get('passed', 0)}/{counts['total']} passed"
                    + (f", {counts['failed']} failed" if counts.get("failed")
                       else ""))
        return self.detail or self.status


class Suite:
    """One component's upstream tests.

    Subclasses fill in the repository, how to run it, and how to read its
    output. Everything else — the workspace, the clone, the timing — is here.
    """

    name = ""
    repo = ""
    #: Does running this say anything about the cluster that was deployed?
    #: False for a suite that builds its own environment, which changes how its
    #: result should be read.
    targets_cluster = True
    #: Commands that must exist on the host, as {binary: why}.
    requires = {}

    def __init__(self, tests=None):
        #: A subset of the suite's own cases, for a suite that has named ones.
        #: Suites that run as a single unit ignore it.
        self.selected = list(tests) if tests else None

    def workspace(self):
        return f"{REGRESSION_ROOT}/{self.name}"

    def source_dir(self):
        return f"{self.workspace()}/src"

    def output_dir(self):
        return f"{self.workspace()}/output"

    # --- 1. can it run here ------------------------------------------
    def missing_requirements(self, executor, plan, node):
        missing = []
        for binary, why in self.requires.items():
            found, _ = executor.try_run(
                f"command -v {shlex.quote(binary)} >/dev/null 2>&1",
                node=node.name,
            )
            if not found:
                missing.append(f"{binary} ({why})")
        return missing

    def install_requirements(self, executor, plan, node, run_logger=None):
        """Try to satisfy what is missing. False when nothing can be done
        automatically — Go and Docker are the operator's call, not ours."""
        return False

    # --- 2. put the suite on the host --------------------------------
    def prepare(self, executor, plan, node, ref, run_logger=None):
        """Clone the component's repository at `ref`. Returns the source dir."""
        source = self.source_dir()
        executor.run(f"mkdir -p {shlex.quote(self.output_dir())}", node=node.name)
        executor.run(f"rm -rf {shlex.quote(source)}", node=node.name)
        executor.run(
            f"git clone --branch {shlex.quote(str(ref))} --depth 1 "
            f"{self.repo} {shlex.quote(source)}",
            node=node.name, message=f"clone {self.name} tests @{ref}",
            timeout=600,
        )
        if run_logger:
            run_logger.info(f"    {self.name}: tests from {self.repo}@{ref}")
        return source

    # --- 3. run it ----------------------------------------------------
    def execute(self, executor, plan, node, run_logger=None):
        raise NotImplementedError

    def parse(self, output):
        """Turn the suite's own output into {total, passed, failed}."""
        return {}

    def artefacts(self, executor, node):
        """Files worth keeping when it fails — diffs, logs, actual output."""
        return []

    # --- the whole thing ----------------------------------------------
    def run(self, executor, plan, node, ref, run_logger=None,
            install_requirements=False):
        result = SuiteResult(component=self.name, ref=str(ref),
                             workspace=self.workspace(),
                             targets_cluster=self.targets_cluster)
        started = time.time()

        missing = self.missing_requirements(executor, plan, node)
        if missing and install_requirements:
            if self.install_requirements(executor, plan, node,
                                         run_logger=run_logger):
                missing = self.missing_requirements(executor, plan, node)
        if missing:
            result.status = SKIPPED
            result.detail = "needs " + ", ".join(missing)
            if not install_requirements and self.install_requirements.__func__ \
                    is not Suite.install_requirements:
                result.detail += " — --install-requirements installs it"
            return result

        try:
            self.prepare(executor, plan, node, ref, run_logger=run_logger)
            ok, output = self.execute(executor, plan, node, run_logger=run_logger)
        except Exception as exc:
            result.status = FAILED
            result.detail = str(exc)[:400]
            result.duration = round(time.time() - started, 1)
            return result

        result.output = output
        result.tests = self.parse(output)
        result.duration = round(time.time() - started, 1)
        failed = result.tests.get("failed")
        # The suite's own tally outranks its exit status: a harness that
        # reports "0 failed" and still exits non-zero has a problem with its
        # own teardown, not with the cluster.
        if failed is not None:
            result.status = PASSED if failed == 0 else FAILED
        else:
            result.status = PASSED if ok else FAILED
        if result.status == FAILED:
            result.detail = result.detail or _last_meaningful_line(output)
            result.artefacts = self.artefacts(executor, node)
        return result


def extension_present(executor, plan, node, name):
    """Is this extension created in the cluster's database on this node?"""
    from aspects import pg_server_management

    literal = "'" + str(name).replace("'", "''") + "'"
    found = pg_server_management.scalar(
        executor, node.bin_dir, node.pg_port, plan.db_user,
        f"SELECT 1 FROM pg_extension WHERE extname = {literal};",
        dbname=plan.db_name, node=node.name, default="",
    )
    return bool((found or "").strip())


def _family_of(plan, node):
    try:
        return plan.host(node.host).family or "rhel"
    except KeyError:
        return "rhel"


def devel_packages_for(plan, node):
    """The package carrying pg_config's makefile for this host and major."""
    from aspects import platform_detect

    major = getattr(node, "pg_version", "") or plan.pg_major
    from aspects import pg_server_management

    major = pg_server_management.major_of(str(major)) or plan.pg_major
    return list(platform_detect.devel_packages(_family_of(plan, node), major))


def pgxs_available(executor, node):
    """Does this host have the development files pg_regress comes with?

    A packaged host can have psql and the server and still lack pg_config's
    makefile, which is what `make installcheck` needs.
    """
    ok, path = executor.try_run(
        f"{shlex.quote(node.bin_dir)}/pg_config --pgxs 2>/dev/null",
        node=node.name,
    )
    if not ok or not path.strip():
        return False
    exists, _ = executor.try_run(f"test -f {shlex.quote(path.strip())}",
                                 node=node.name)
    return bool(exists)


def _last_meaningful_line(output):
    """The last line that looks like it says something, for a one-line detail."""
    for line in reversed((output or "").strip().splitlines()):
        text = line.strip()
        if text and not set(text) <= set("=-_ "):
            return text[:300]
    return "no output"


# ---------------------------------------------------------------------------
# pg_regress, which two of the four use
# ---------------------------------------------------------------------------


PG_REGRESS_PATTERNS = (
    # "All 23 tests passed." / "1 of 23 tests failed."
    (r"All (\d+) tests? passed", lambda m: {"total": int(m.group(1)),
                                            "passed": int(m.group(1)),
                                            "failed": 0}),
    (r"(\d+) of (\d+) tests? failed", lambda m: {"total": int(m.group(2)),
                                                 "failed": int(m.group(1)),
                                                 "passed": int(m.group(2))
                                                 - int(m.group(1))}),
)


def parse_pg_regress(output):
    """pg_regress prints its tally in a banner at the end."""
    for pattern, build in PG_REGRESS_PATTERNS:
        match = re.search(pattern, output or "")
        if match:
            return build(match)
    return {}


class PgRegressSuite(Suite):
    """A PGXS extension whose tests run against a live server.

    `make installcheck` points pg_regress at whatever PGHOST/PGPORT/PGUSER say,
    so this is the form that tests the cluster actually deployed rather than a
    temporary instance built for the occasion.
    """

    requires = {"git": "to fetch the suite", "make": "to run installcheck"}

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        if not pgxs_available(executor, node):
            # Name the package rather than the symptom: "the -devel package"
            # leaves the reader to work out which one, and the answer differs
            # per family and per major.
            packages = devel_packages_for(plan, node)
            missing.append(
                f"pg_config --pgxs, from {' or '.join(packages)}"
                if packages else "pg_config --pgxs (the -devel/-dev package)"
            )
        return missing

    def install_requirements(self, executor, plan, node, run_logger=None):
        """Install the development files pg_regress needs. Returns True if
        anything was attempted."""
        from aspects import package_management

        packages = devel_packages_for(plan, node)
        if not packages:
            return False
        family = _family_of(plan, node)
        if run_logger:
            run_logger.info(f"    {self.name}: installing "
                            f"{', '.join(packages)}")
        package_management.install(executor, family, packages, node=node.name,
                                   allow_missing=True)
        return True
    #: pg_regress creates this database and drops it again.
    regress_db = "regression"

    def install_target(self):
        return "installcheck"

    def environment(self, plan, node):
        return {
            "USE_PGXS": "1",
            "PATH": f"{node.bin_dir}:$PATH",
            "PGHOST": node.address,
            "PGPORT": str(node.pg_port),
            "PGUSER": plan.db_user,
            "PGDATABASE": plan.db_name,
        }

    def execute(self, executor, plan, node, run_logger=None):
        env = " ".join(f"{key}={value}" for key, value in
                       self.environment(plan, node).items())
        log = f"{self.output_dir()}/installcheck.log"
        if run_logger:
            run_logger.info(f"    {self.name}: make {self.install_target()} "
                            f"against {node.name} "
                            f"({node.address}:{node.pg_port})")
        ok, output = executor.try_run(
            f"cd {shlex.quote(self.source_dir())} && "
            f"{env} make {self.install_target()} 2>&1 | tee {shlex.quote(log)}",
            node=node.name, timeout=3600,
        )
        return ok, output

    def parse(self, output):
        return parse_pg_regress(output)

    def artefacts(self, executor, node):
        """regression.diffs is the file that says what differed."""
        found = []
        for candidate in ("regression.diffs", "regression.out",
                          "results", "tests/regress/regression.diffs",
                          "tests/regress/regression_output"):
            path = f"{self.source_dir()}/{candidate}"
            exists, _ = executor.try_run(f"test -e {shlex.quote(path)}",
                                         node=node.name)
            if exists:
                found.append(path)
        return found


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------


def _load_suites():
    from regression import ace, lolor, snowflake, spock

    return {
        spock.SpockSuite.name: spock.SpockSuite,
        lolor.LolorSuite.name: lolor.LolorSuite,
        snowflake.SnowflakeSuite.name: snowflake.SnowflakeSuite,
        ace.AceSuite.name: ace.AceSuite,
    }


class _Registry(dict):
    """Filled on first use: the suite modules import from this one."""

    def _ensure(self):
        if not self:
            self.update(_load_suites())
        return self

    def __getitem__(self, key):
        return dict.__getitem__(self._ensure(), key)

    def __contains__(self, key):
        return dict.__contains__(self._ensure(), key)

    def keys(self):
        return dict.keys(self._ensure())

    def items(self):
        return dict.items(self._ensure())


SUITES = _Registry()

ALIASES = {"pgedge-lolor": "lolor", "pgedge-snowflake": "snowflake",
           "pgedge-ace": "ace", "pgedge-spock": "spock",
           "spock50": "spock", "spock60": "spock"}


def known_components():
    return sorted(SUITES.keys())


def suite_for(name, tests=None):
    key = ALIASES.get(str(name).strip().lower(), str(name).strip().lower())
    if key not in SUITES:
        raise ValueError(
            f"unknown component {name!r}. Available: "
            f"{', '.join(known_components())} (or 'all')"
        )
    return SUITES[key](tests=tests)


def parse_components(value):
    """Read a --health_check value into a list of suite names.

    'all' is every suite this tool knows how to run, not every component the
    cluster happens to have: a cluster without lolor still answers the question
    "is lolor's suite green here?" with "it is not installed", which is more
    useful than silently dropping it.
    """
    text = str(value or "").strip()
    if not text:
        return []
    if text.lower() in ("all", "*"):
        return known_components()

    chosen, seen = [], set()
    for raw in re.split(r"[,\s]+", text):
        if not raw:
            continue
        suite = suite_for(raw)
        if suite.name not in seen:
            seen.add(suite.name)
            chosen.append(suite.name)
    return chosen
