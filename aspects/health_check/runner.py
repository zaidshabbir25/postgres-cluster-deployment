#!/usr/bin/env python3
"""The contract every component's health check implements.

A suite is asked three questions in order, and each has a distinct answer worth
reporting:

  1. `missing_requirements` — can this run here at all? A host without the ACE
     binary cannot run ACE's checks, and saying "skipped: no ace" is a
     different fact from "failed".
  2. `prepare` — put what the suite needs on the host: cases from this
     repository, an upstream checkout at the ref the component was built from,
     or a configuration file naming the cluster's nodes.
  3. `execute` — run it, and turn its output into counts.

Two kinds of suite live here, and the difference decides how a result reads:

  * **In-tree cases** (`InTreeSuite`: spock, lolor) ship with this tool and are
    written for a cluster that is already running. They create nothing but a
    schema of their own, find the peers through `spock.node_interface`, and
    read them through dblink in one session. Their expected files are fixed,
    so a healthy cluster matches and a broken one diffs with the node named.
  * **Upstream harnesses** (snowflake, ace-integration) are fetched at a ref
    and run the way their own project intends. Whether they look at this
    cluster at all is recorded in `targets_cluster`, and the report says so.

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
    #: For a suite that does not target the cluster: what its verdict is
    #: about, in words the report can print.
    scope: str = ""
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
    #: When targets_cluster is False: what the verdict is about instead.
    scope = ""
    #: Commands that must exist on the host, as {binary: why}.
    requires = {}

    def __init__(self, tests=None, test_timeout=None):
        #: A subset of the suite's own cases, for a suite that has named ones.
        #: Suites that run as a single unit ignore it.
        self.selected = list(tests) if tests else None
        if test_timeout:
            self.test_timeout = int(test_timeout)

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
                             targets_cluster=self.targets_cluster,
                             scope=self.scope)
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


def _lit(value):
    """Quote a Python value as a SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


class InTreeSuite(Suite):
    """Cases that ship with this tool, written for a cluster already running.

    A subclass names its directory and the settings prefix its SQL reads the
    node name from. Everything else — copying the cases over, running each in
    its own psql session, diffing against the expected file, finding the first
    line that explains a failure — is here, so the spock and lolor suites
    differ only in their cases.
    """

    repo = ""          # nothing to clone: the cases live in this repository
    requires = {}      # and psql is the only thing they need
    #: Where the sql/ and expected/ directories are, as a pathlib.Path.
    suite_dir = None
    #: The cases read the node name and count as current_setting('<prefix>.node')
    #: and current_setting('<prefix>.node_count').
    settings_prefix = "health_check"
    #: Each case waits for replication rather than assuming it, so the limit is
    #: about a cluster that is not replicating at all, not about a slow one.
    test_timeout = 180

    @classmethod
    def sql_dir(cls):
        return cls.suite_dir / "sql"

    @classmethod
    def expected_dir(cls):
        return cls.suite_dir / "expected"

    @classmethod
    def available_tests(cls):
        """Case names, in the order their filenames give them. Numbered,
        because they depend on each other: one creates what the next uses and
        the last removes it again."""
        return [path.stem for path in sorted(cls.sql_dir().glob("*.sql"))]

    # --- can it run here ----------------------------------------------
    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        found, _ = executor.try_run(
            f"test -x {shlex.quote(node.bin_dir)}/psql", node=node.name)
        if not found:
            missing.append(f"psql at {node.bin_dir}")
        return missing

    # --- it is already here -------------------------------------------
    def prepare(self, executor, plan, node, ref, run_logger=None):
        """Copy the cases onto the host. There is nothing to fetch."""
        remote_sql = f"{self.workspace()}/sql"
        remote_expected = f"{self.workspace()}/expected"
        executor.run(f"mkdir -p {shlex.quote(remote_sql)} "
                     f"{shlex.quote(remote_expected)} "
                     f"{shlex.quote(self.output_dir())}", node=node.name)

        for local, remote in ((self.sql_dir(), remote_sql),
                              (self.expected_dir(), remote_expected)):
            for path in sorted(local.iterdir()):
                if path.is_file():
                    executor.put_file(path, f"{remote}/{path.name}",
                                      owner=plan.db_user, mode="644",
                                      node=node.name)
        if run_logger:
            run_logger.info(
                f"    {self.name}: {len(self.available_tests())} case(s) from "
                f"this repository, against the running cluster — nothing is "
                f"created")
        return remote_sql

    def tests_for(self, plan, node):
        chosen = self.selected or self.available_tests()
        known = set(self.available_tests())
        return [name for name in chosen if name in known] or list(chosen)

    # --- running ------------------------------------------------------
    def execute(self, executor, plan, node, run_logger=None):
        results = f"{self.output_dir()}/results"
        executor.run(f"mkdir -p {shlex.quote(results)}", node=node.name)

        chosen = self.tests_for(plan, node)
        lines = []
        passed = failed = 0

        if run_logger:
            run_logger.info(
                f"    {self.name}: {len(chosen)} case(s) on {node.name} as "
                f"{plan.db_user}, {self.test_timeout}s each")

        for index, name in enumerate(chosen, start=1):
            verdict = self._run_one(executor, plan, node, name, results)
            lines.append(f"test {name:<28} ... {verdict}")
            if verdict == "ok":
                passed += 1
            else:
                failed += 1
            if run_logger:
                run_logger.info(
                    f"      [{index}/{len(chosen)}] {name:<28} {verdict}")

        total = passed + failed
        if failed:
            lines.append(f"{failed} of {total} tests failed.")
        elif total:
            lines.append(f"All {total} tests passed.")
        return failed == 0, "\n".join(lines)

    def _run_one(self, executor, plan, node, name, results):
        sql = f"{self.workspace()}/sql/{name}.sql"
        expected = f"{self.workspace()}/expected/{name}.out"
        actual = f"{results}/{name}.out"

        # -A -t: values only, so the expected files hold what the case asserts
        # rather than an echo of how it asked. -w: never prompt, so a missing
        # password fails at once instead of consuming the whole timeout.
        # Settings rather than psql variables: psql does not interpolate
        # :name inside a dollar-quoted body, and the cases need the same value
        # in both places. Separate -c arguments, so each is its own statement
        # in the session the -f then runs in.
        prefix = self.settings_prefix
        set_node = f"SET {prefix}.node = {_lit(node.name)}"
        set_count = f"SET {prefix}.node_count = {len(plan.spock_nodes)}"
        settings = (f"-c {shlex.quote(set_node)} "
                    f"-c {shlex.quote(set_count)} ")
        try:
            executor.try_run(
                f"{shlex.quote(node.bin_dir)}/psql -X -q -w -A -t "
                f"-v ON_ERROR_STOP=0 "
                f"-h {shlex.quote(node.address)} -p {node.pg_port} "
                f"-U {shlex.quote(plan.db_user)} "
                f"-d {shlex.quote(plan.db_name)} "
                f"{settings}-f {shlex.quote(sql)} "
                f"> {shlex.quote(actual)} 2>&1",
                node=node.name, timeout=self.test_timeout,
            )
        except Exception as exc:
            if "Timeout" in type(exc).__name__:
                return f"TIMED OUT after {self.test_timeout}s"
            return f"FAILED ({type(exc).__name__})"

        same, _ = executor.try_run(
            f"diff -u {shlex.quote(expected)} {shlex.quote(actual)} "
            f"> {shlex.quote(results)}/{name}.diff 2>&1", node=node.name)
        if same:
            return "ok"
        return f"FAILED{self._first_problem(executor, node, actual)}"

    def _first_problem(self, executor, node, actual):
        """The first thing that looks like an explanation.

        An ERROR if there is one; otherwise the first assertion that came back
        false, or the first line naming a node as disagreeing. Forty rows
        saying FAILED say only that something is wrong.
        """
        ok, line = executor.try_run(
            f"grep -m1 -E '^(psql:.*)?(ERROR|FATAL):|\\|false$"
            f"|^(DOWN|DISAGREES|NOT_REPLICATED)\\|' "
            f"{shlex.quote(actual)} 2>/dev/null", node=node.name)
        text = (line or "").strip()
        if not ok or not text:
            return ""
        # psql's "file:line:" prefix says where, which the diff already does;
        # the message is what explains.
        text = re.sub(r"^psql:[^:]*:\d+:\s*", "", text)
        return f" — {text[:120]}"

    def parse(self, output):
        passed = len(re.findall(r"\.\.\. ok$", output or "", re.MULTILINE))
        failed = len(re.findall(r"\.\.\. (FAILED|TIMED OUT)", output or "",
                                re.MULTILINE))
        if not (passed or failed):
            return {}
        return {"total": passed + failed, "passed": passed, "failed": failed}

    def artefacts(self, executor, node):
        """Each failed case leaves its actual output and diff."""
        results = f"{self.output_dir()}/results"
        _, listing = executor.try_run(
            f"ls -1 {shlex.quote(results)}/*.diff 2>/dev/null", node=node.name)
        kept = []
        for path in (listing or "").splitlines():
            path = path.strip()
            if not path:
                continue
            nonempty, _ = executor.try_run(f"test -s {shlex.quote(path)}",
                                           node=node.name)
            if nonempty:
                kept.append(path)
        return kept


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
    """A PGXS extension's own pg_regress suite, fetched at a ref and run on the
    host with the installed library.

    Both extensions this covers start a scratch instance for their tests —
    lolor's Makefile says so in REGRESS_OPTS, spock's regresscheck target does
    it by hand — so the verdict is about the library the nodes run, exercised
    in isolation, not about the mesh. That instance is initdb'd by pg_regress,
    and initdb refuses to run as root, so the checkout is handed to the
    database user and make runs as that user.

    What is never run here is upstream's `check` target: for spock it is
    `check: install regresscheck`, which would install the checkout's spock.so
    over the one every node is running.
    """

    requires = {"git": "to fetch the suite", "make": "to run the suite"}
    targets_cluster = False
    scope = "a scratch instance on the host, with the installed library"

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
        # No PGHOST/PGPORT: the suite starts its own instance and must not be
        # pointed at a node of the cluster.
        return {
            "USE_PGXS": "1",
            "PATH": f"{node.bin_dir}:/usr/local/bin:/usr/bin:/bin",
            "HOME": self.workspace(),
        }

    def prepare(self, executor, plan, node, ref, run_logger=None):
        source = super().prepare(executor, plan, node, ref, run_logger=run_logger)
        # The database user runs the suite: pg_regress initdb's a scratch
        # instance inside the checkout, and initdb refuses root.
        executor.run(
            f"chown -R {plan.db_user}:{plan.db_user} "
            f"{shlex.quote(self.workspace())}", node=node.name)
        return source

    def execute(self, executor, plan, node, run_logger=None):
        env = " ".join(f"{key}={value}" for key, value in
                       self.environment(plan, node).items())
        log = f"{self.output_dir()}/{self.install_target()}.log"
        if run_logger:
            run_logger.info(f"    {self.name}: make {self.install_target()} "
                            f"as {plan.db_user} on {node.name}, in a scratch "
                            f"instance with the installed library")
        ok, output = executor.try_run(
            f"cd {shlex.quote(self.source_dir())} && "
            f"{env} make {self.install_target()} > {shlex.quote(log)} 2>&1; "
            f"rc=$?; cat {shlex.quote(log)}; exit $rc",
            user=plan.db_user, node=node.name, timeout=3600,
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
    from aspects.health_check import (ace, ace_integration, lolor,
                                      lolor_upstream, snowflake, spock,
                                      spock_upstream)

    return {
        spock.SpockSuite.name: spock.SpockSuite,
        spock_upstream.SpockUpstreamSuite.name: spock_upstream.SpockUpstreamSuite,
        lolor.LolorSuite.name: lolor.LolorSuite,
        lolor_upstream.LolorUpstreamSuite.name: lolor_upstream.LolorUpstreamSuite,
        snowflake.SnowflakeSuite.name: snowflake.SnowflakeSuite,
        ace.AceSuite.name: ace.AceSuite,
        ace_integration.AceIntegrationSuite.name:
            ace_integration.AceIntegrationSuite,
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

#: A component's name means everything that checks it: the cases written for
#: the running cluster *and* upstream's own suite, fetched fresh at the ref
#: the cluster was built from. Either half can still be asked for by name.
GROUPS = {
    "spock": ("spock", "spock-upstream"),
    "lolor": ("lolor", "lolor-upstream"),
}

#: Never part of 'all': it starts containers of its own, takes most of an
#: hour, and says nothing about this cluster. By name only.
BY_NAME_ONLY = ("ace-integration",)


def known_components():
    return sorted(SUITES.keys())


def default_components():
    """What 'all' means: every suite except the ones that run by name only."""
    return [name for name in known_components() if name not in BY_NAME_ONLY]


def upstream_components():
    """What 'upstream' means: each component's own suite, fetched at its ref."""
    return [name for name in known_components() if name.endswith("-upstream")]


def suite_for(name, tests=None, test_timeout=None):
    key = ALIASES.get(str(name).strip().lower(), str(name).strip().lower())
    if key not in SUITES:
        raise ValueError(
            f"unknown component {name!r}. Available: "
            f"{', '.join(known_components())} (or 'all')"
        )
    return SUITES[key](tests=tests, test_timeout=test_timeout)


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

    names = []

    def add(name):
        if name not in names:
            names.append(name)

    for raw in re.split(r"[,\s]+", text):
        if not raw:
            continue
        word = raw.strip().lower()
        if word == "all":
            for name in default_components():
                add(name)
            continue
        if word == "upstream":
            for name in upstream_components():
                add(name)
            continue
        key = ALIASES.get(word, word)
        if key in GROUPS:
            for name in GROUPS[key]:
                add(name)
            continue
        if key not in SUITES:
            raise ValueError(
                f"unknown component {raw!r}. Available: "
                f"{', '.join(known_components())}, a component name for "
                f"everything that checks it, 'upstream', or 'all'"
            )
        add(key)
    return names
