#!/usr/bin/env python3
"""Fail a Spock node over and ask whether the mesh survives it.

Every other health check takes the cluster as it stands. This one changes it:
it stops the leader of a scope that has a standby, waits for Patroni to
promote the standby, and checks what a promotion has to leave behind for the
peers to carry on — then hands the scope back with a switchover, so the
cluster ends in the shape it began. It runs by name only, never as part of
`all`, because a suite that fails a leader over is not something to start
without having asked for it.

What a promotion must leave behind, and what each case asks:

  * The promoted member has the Spock slots the peers' apply workers connect
    to. Spock keeps a copy of each `spk_*` slot on the standby for exactly this
    moment; a copy that is temporary, or missing, is dropped at promotion and
    the peers loop on "replication slot does not exist". `01_standby_slots`
    asks the standby about its copies before anything is failed over, so a
    copy that would not survive is reported with its state rather than as a
    mystery afterwards.
  * The peers point at the new primary. Spock has no notion of a scope, so
    every peer still holds the DSN of the member that just went down. The
    on_role_change callback repairs that when automatic repair is on; the
    suite waits for it, and repairs by hand when it is off or late, saying
    which.
  * Rows written before the failover are on the new primary; rows written
    after it reach every peer and come back from them; and the old leader
    rejoins its scope as a replica once it is started again.

Three things the suite will not do. It will not take a leader down whose
standby is not streaming — the scope would be left with no leader. It will
not touch a scope without a standby: it is skipped with the command that adds
one. And it creates nothing but a schema of its own, dropped by the last case.
"""

import re
import shlex
import time

from aspects.health_check.runner import Suite, _lit

#: The schema the cases write into, dropped again by the last one.
SCHEMA = "failover_regress"
TABLE = f"{SCHEMA}.rows"

#: Roles patronictl prints for the member that leads a scope.
LEADER_ROLES = ("leader", "master", "primary")
#: Roles for a member that follows it.
FOLLOWER_ROLES = ("replica", "sync standby", "sync_standby", "standby")
#: Member states that mean "serving".
SERVING_STATES = ("running", "streaming", "in archive recovery")

#: Seconds between polls of a wait.
POLL_INTERVAL = 5


def _now():
    return time.monotonic()


def _sleep(seconds):
    """The one place the suite waits, so a test can run the clock itself."""
    time.sleep(seconds)


class CheckFailed(Exception):
    """A case's assertion that did not hold; the message explains."""


class FailoverSuite(Suite):
    name = "failover"
    repo = ""          # nothing to clone: the scenarios are this module
    requires = {}      # psql on the node, probed below
    targets_cluster = True
    #: How long one wait may take: for a promotion, for a member to rejoin,
    #: for a row to arrive. A failover that takes longer is the finding.
    test_timeout = 300

    CASES = ("01_standby_slots", "02_unplanned_failover",
             "03_switchover_back", "04_cleanup")

    def __init__(self, tests=None, test_timeout=None, executors=None):
        super().__init__(tests=tests, test_timeout=test_timeout)
        #: Executors by host name, for members and peers on other hosts than
        #: the one the health check runs from. Built on demand and closed
        #: when the suite finishes; a caller may pass its own.
        self.executors = dict(executors or {})
        self._owned = []
        self.log = None

    @classmethod
    def available_tests(cls):
        return list(cls.CASES)

    # --- what is exercised ----------------------------------------------
    @staticmethod
    def subject(plan, node):
        """The Spock node whose scope is failed over: the node the check runs
        on if it has a standby, else the first that does."""
        if node.is_spock and node.standbys:
            return node
        return next((n for n in plan.spock_nodes if n.standbys), None)

    @staticmethod
    def peers(plan, subject):
        return [n for n in plan.spock_nodes if n.name != subject.name]

    # --- 1. can it run here ---------------------------------------------
    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        found, _ = executor.try_run(
            f"test -x {shlex.quote(node.bin_dir)}/psql", node=node.name)
        if not found:
            missing.append(f"psql at {node.bin_dir}")
        subject = self.subject(plan, node)
        if subject is None:
            first = plan.spock_nodes[0].name if plan.spock_nodes else "n1"
            missing.append(
                f"a Spock node with a standby to fail over to "
                f"(add-standby --leader {first})")
        elif not self.peers(plan, subject):
            missing.append(
                f"a peer of {subject.name} to replicate with (the cluster "
                f"has one Spock node)")
        return missing

    # --- 2. nothing to put on the host ------------------------------------
    def prepare(self, executor, plan, node, ref, run_logger=None):
        executor.run(f"mkdir -p {shlex.quote(self.output_dir())}",
                     node=node.name)
        subject = self.subject(plan, node)
        if run_logger and subject is not None:
            repair = ("the on_role_change callback"
                      if getattr(plan, "auto_repair_failover", False)
                      else "this suite, by hand (auto-repair is off)")
            run_logger.info(
                f"    {self.name}: scope {subject.scope} — {subject.name} "
                f"with standby {', '.join(subject.standbys)}; peers "
                f"{', '.join(p.name for p in self.peers(plan, subject))}; "
                f"peers retargeted by {repair}")
        return self.output_dir()

    # --- 3. run it ---------------------------------------------------------
    def tests_for(self, plan, node):
        chosen = self.selected or self.available_tests()
        known = set(self.available_tests())
        return [name for name in chosen if name in known] or list(chosen)

    def execute(self, executor, plan, node, run_logger=None):
        self.log = run_logger
        self._run_node = node
        self._run_executor = executor
        self._plan = plan
        subject = self.subject(plan, node)
        chosen = self.tests_for(plan, node)
        lines = []
        passed = failed = 0

        if run_logger:
            run_logger.info(
                f"    {self.name}: {len(chosen)} scenario(s) on scope "
                f"{subject.scope}, {self.test_timeout}s per wait")

        runners = {
            "01_standby_slots": self.case_standby_slots,
            "02_unplanned_failover": self.case_unplanned_failover,
            "03_switchover_back": self.case_switchover_back,
            "04_cleanup": self.case_cleanup,
        }
        try:
            for index, name in enumerate(chosen, start=1):
                checks = []
                try:
                    runners[name](subject, checks)
                    verdict = "ok"
                except CheckFailed as exc:
                    verdict = f"FAILED — {str(exc)[:300]}"
                except Exception as exc:      # noqa: BLE001 - reported per case
                    verdict = f"FAILED ({type(exc).__name__}: {str(exc)[:120]})"
                lines.append(f"test {name:<28} ... {verdict}")
                lines.extend(f"  {label}|{str(ok).lower()}"
                             + (f"  {detail}" if detail else "")
                             for label, ok, detail in checks)
                if verdict == "ok":
                    passed += 1
                else:
                    failed += 1
                if run_logger:
                    run_logger.info(
                        f"      [{index}/{len(chosen)}] {name:<28} {verdict}")
        finally:
            for owned in self._owned:
                try:
                    owned.close()
                except Exception:
                    pass
            self._owned = []

        total = passed + failed
        if failed:
            lines.append(f"{failed} of {total} tests failed.")
        elif total:
            lines.append(f"All {total} tests passed.")
        return failed == 0, "\n".join(lines)

    def parse(self, output):
        passed = len(re.findall(r"\.\.\. ok$", output or "", re.MULTILINE))
        failed = len(re.findall(r"\.\.\. FAILED", output or "", re.MULTILINE))
        if not (passed or failed):
            return {}
        return {"total": passed + failed, "passed": passed, "failed": failed}

    # ------------------------------------------------------------------
    # reaching the members
    # ------------------------------------------------------------------

    def executor_for(self, node):
        """The executor for a node's host, built when the node lives on a
        host other than the one the check runs from."""
        if node.host == self._run_node.host:
            return self._run_executor
        if node.host not in self.executors:
            from aspects.ssh_executor import build_executor

            host = self._plan.host(node.host)
            executor = build_executor(
                {
                    "name": host.name,
                    "host": host.address,
                    "username": host.username,
                    "key_file": host.key_file,
                    "port": host.port,
                    "local": host.local,
                },
                run_logger=self.log,
            )
            executor.connect()
            self.executors[node.host] = executor
            self._owned.append(executor)
        return self.executors[node.host]

    def scalar(self, node, sql, default=None):
        from aspects import pg_server_management

        return pg_server_management.scalar(
            self.executor_for(node), node.bin_dir, node.pg_port,
            self._plan.db_user, sql, dbname=self._plan.db_name,
            node=node.name, default=default)

    def sql(self, node, statement):
        """Run a statement that must succeed. Returns its output."""
        from aspects import pg_server_management

        code, output = pg_server_management.psql(
            self.executor_for(node), node.bin_dir, node.pg_port,
            self._plan.db_user, statement, dbname=self._plan.db_name,
            node=node.name, check=False)
        if code != 0:
            raise CheckFailed(
                f"{node.name}: {statement[:60]} — {output.strip()[:120]}")
        return output

    def members(self, via):
        """The scope's members as patronictl on `via` reports them."""
        from aspects import patroni_management

        return patroni_management.list_members(
            self.executor_for(via), via, node_name=via.name)

    def scope_members(self, subject):
        """The plan's nodes in the subject's scope: the leader and its
        standbys, as Node objects."""
        return [n for n in self._plan.nodes if n.scope == subject.scope]

    def leader_member(self, subject, via=None):
        """Which member leads the subject's scope right now, as a Node."""
        for candidate in ([via] if via else []) + self.scope_members(subject):
            for member in self.members(candidate):
                if (member.get("role") or "").lower() in LEADER_ROLES:
                    try:
                        return self._plan.node(member["name"])
                    except (KeyError, ValueError):
                        return None
            # patronictl answered with no leader, or did not answer: ask the
            # next member before concluding there is none.
        return None

    def member_state(self, name, via):
        for member in self.members(via):
            if member.get("name") == name:
                return ((member.get("role") or "").lower(),
                        (member.get("state") or "").lower())
        return None, None

    # ------------------------------------------------------------------
    # waiting
    # ------------------------------------------------------------------

    def wait(self, what, condition, timeout=None):
        """Poll `condition` until it returns something true, or give up.
        Returns (value, seconds) and raises CheckFailed on timeout."""
        timeout = timeout or self.test_timeout
        started = _now()
        last = None
        while True:
            last = condition()
            if last:
                return last, int(_now() - started)
            if _now() - started >= timeout:
                raise CheckFailed(f"{what} not within {timeout}s")
            _sleep(POLL_INTERVAL)

    def wait_for_leader(self, subject, expected, via):
        """Until `expected` leads the scope and is serving."""
        def promoted():
            role, state = self.member_state(expected.name, via)
            return role in LEADER_ROLES and state in SERVING_STATES

        _, seconds = self.wait(f"promotion of {expected.name}", promoted)
        return seconds

    def wait_for_follower(self, member, via):
        """Until `member` follows the leader again and is streaming."""
        def rejoined():
            role, state = self.member_state(member.name, via)
            return role in FOLLOWER_ROLES and state in SERVING_STATES

        _, seconds = self.wait(f"{member.name} rejoining as a replica", rejoined)
        return seconds

    def wait_for_rows(self, node, phase, count, what):
        def arrived():
            value = self.scalar(
                node, f"SELECT count(*) FROM {TABLE} WHERE phase = {_lit(phase)};",
                default="")
            return (value or "").strip() == str(count)

        _, seconds = self.wait(what, arrived)
        return seconds

    # ------------------------------------------------------------------
    # Spock on the members
    # ------------------------------------------------------------------

    def spock_slots(self, node):
        """(count, temporary_count) of spk_* logical slots on a member."""
        value = self.scalar(
            node,
            "SELECT count(*) || '|' || count(*) FILTER (WHERE temporary) "
            "FROM pg_replication_slots "
            "WHERE slot_type = 'logical' AND slot_name LIKE 'spk\\_%';",
            default="")
        parts = (value or "").strip().split("|")
        try:
            return int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            return 0, 0

    def slot_sync_hint(self, standby):
        """Why a Spock 6 standby on PostgreSQL 17+ may have no copies: the
        only mechanism that makes them there is PostgreSQL's own slot sync,
        and it is off unless the DCS says otherwise."""
        from aspects import pg_server_management

        major = pg_server_management.major_of(
            getattr(standby, "pg_version", "") or self._plan.pg_version
            or self._plan.pg_major)
        if str(self._plan.spock_major) != "60" or not major or int(major) < 17:
            return ""
        setting = self.scalar(standby, "SHOW sync_replication_slots;",
                              default="")
        if (setting or "").strip().lower() == "on":
            return ""
        return (f"; sync_replication_slots is off on {standby.name}, and on "
                f"PostgreSQL {major} Spock 6 copies slots only through it: "
                f"db guc-set sync_replication_slots on")

    def peer_subscription(self, peer, subject):
        """Status of the peer's subscription to the subject, as Spock says."""
        from aspects import spock_management

        wanted = spock_management.sub_name(subject.name, peer.name)
        for sub in spock_management.subscription_status(
                self.executor_for(peer), self._plan, peer):
            if sub["name"] == wanted:
                return (sub["status"] or "").lower()
        return "missing"

    def peers_replicating(self, subject):
        stale = []
        for peer in self.peers(self._plan, subject):
            status = self.peer_subscription(peer, subject)
            if status not in ("replicating", "running"):
                stale.append(f"{peer.name}={status}")
        return stale

    def retarget(self, subject, promoted):
        """Point every peer's subscription at the promoted member."""
        from aspects import spock_management

        address = promoted.advertise_address or promoted.address
        results = spock_management.retarget_after_failover(
            self.executor_for, self._plan, subject, address, promoted.pg_port,
            run_logger=self.log)
        failed = [f"{r['peer']}: {r['step']}" for r in results if not r["ok"]]
        if failed:
            raise CheckFailed("retargeting the peers failed — "
                              + "; ".join(failed)[:140])

    def repaired(self, subject, promoted, checks):
        """Until every peer replicates from the promoted member again.

        With automatic repair on, the callback on the promoted member does
        the retargeting and the suite only waits. Off, or when the callback
        has not done it within half the timeout, the suite retargets the
        peers itself — and says so, since a callback that did not fire is a
        finding of its own.
        """
        auto = getattr(self._plan, "auto_repair_failover", False)
        if auto:
            try:
                _, seconds = self.wait(
                    "peers retargeted by the callback",
                    lambda: not self.peers_replicating(subject),
                    timeout=max(self.test_timeout // 2, POLL_INTERVAL))
                checks.append(("peers_retargeted_by_callback", True,
                               f"in {seconds}s"))
                return
            except CheckFailed:
                checks.append(("peers_retargeted_by_callback", False,
                               "stale: " + ", ".join(
                                   self.peers_replicating(subject))))
                # fall through: repair by hand so the rest can be asked
        self.retarget(subject, promoted)
        _, seconds = self.wait(
            "peers replicating after retargeting",
            lambda: not self.peers_replicating(subject))
        checks.append(("peers_retargeted_by_hand", True, f"in {seconds}s"))

    def check_promoted(self, subject, promoted, checks):
        """What a member must have the moment it leads the scope: it accepts
        writes, and it holds the slots the peers will connect to."""
        recovery = self.scalar(promoted, "SELECT pg_is_in_recovery();",
                               default="?")
        checks.append(("new_primary_accepts_writes", recovery == "f",
                       f"{promoted.name} pg_is_in_recovery={recovery}"))
        if recovery != "f":
            raise CheckFailed(f"{promoted.name} is still in recovery")

        peers = self.peers(self._plan, subject)
        slots, temporary = self.spock_slots(promoted)
        enough = slots >= len(peers) and temporary == 0
        checks.append(("new_primary_has_spock_slots", enough,
                       f"{slots} spk_* slot(s) for {len(peers)} peer(s)"
                       + (f", {temporary} temporary" if temporary else "")))
        if not enough:
            # The peers cannot reconnect without the slots; retargeting
            # them would only produce the "does not exist" loop. Say what
            # was found and stop this case here.
            raise CheckFailed(
                f"{promoted.name} has {slots} spk_* slot(s) after promotion, "
                f"{len(peers)} needed — the standby's copies did not survive")

    def check_mesh(self, subject, promoted, baseline, checks):
        """Once the scope is whole again — its former leader back as a
        replica — the peers must follow the new primary and rows must travel
        both ways. Not before: a scope in strict synchronous mode holds every
        commit, the apply workers' included, until a standby is streaming.
        """
        peers = self.peers(self._plan, subject)
        self.repaired(subject, promoted, checks)

        before = self.scalar(
            promoted,
            f"SELECT count(*) FROM {TABLE} WHERE phase = {_lit(baseline)};",
            default="")
        checks.append(("no_data_loss", (before or "").strip() == "100",
                       f"{(before or '?').strip()} of 100 pre-failover rows "
                       f"on {promoted.name}"))

        # Ids in a range of their own per writer, so writers that have not
        # yet seen each other's rows cannot collide on a key.
        phase = f"after-{promoted.name}"
        base = 1000 * (1 + self.scope_members(subject).index(promoted))
        self.sql(promoted,
                 f"INSERT INTO {TABLE} (id, phase) "
                 f"SELECT g + {base}, {_lit(phase)} "
                 f"FROM generate_series(1, 20) g;")
        for peer in peers:
            seconds = self.wait_for_rows(
                peer, phase, 20, f"rows from {promoted.name} reaching {peer.name}")
            checks.append(("forward_replication", True,
                           f"{peer.name} in {seconds}s"))

        back = f"back-{promoted.name}"
        for index, peer in enumerate(peers, start=1):
            self.sql(peer,
                     f"INSERT INTO {TABLE} (id, phase) "
                     f"SELECT g + {base + 100 * index}, {_lit(back)} "
                     f"FROM generate_series(1, 10) g;")
            seconds = self.wait_for_rows(
                promoted, back, 10 * index,
                f"rows from {peer.name} reaching {promoted.name}")
            checks.append(("reverse_replication", True,
                           f"from {peer.name} in {seconds}s"))

    # ------------------------------------------------------------------
    # the cases
    # ------------------------------------------------------------------

    def seed(self, subject, leader, checks):
        """A table in the suite's schema, 100 rows, confirmed on every peer."""
        self.sql(leader, f"SELECT spock.replicate_ddl("
                         f"'CREATE SCHEMA IF NOT EXISTS {SCHEMA}');")
        self.sql(leader, f"SELECT spock.replicate_ddl($ddl$CREATE TABLE IF NOT "
                         f"EXISTS {TABLE} (id int PRIMARY KEY, phase text)$ddl$);")
        in_set = self.scalar(
            leader,
            f"SELECT 1 FROM spock.tables WHERE nspname = {_lit(SCHEMA)} "
            f"AND relname = 'rows' AND set_name = 'default';", default="")
        if not (in_set or "").strip():
            self.sql(leader, f"SELECT spock.repset_add_table('default', "
                             f"{_lit(TABLE)});")
        self.sql(leader, f"DELETE FROM {TABLE};")
        self.sql(leader, f"INSERT INTO {TABLE} (id, phase) "
                         f"SELECT g, 'before' FROM generate_series(1, 100) g;")
        for peer in self.peers(self._plan, subject):
            seconds = self.wait_for_rows(
                peer, "before", 100, f"baseline reaching {peer.name}")
            checks.append(("baseline_replicated", True,
                           f"{peer.name} in {seconds}s"))

    def case_standby_slots(self, subject, checks):
        """Before anything is failed over: does the standby carry a copy of
        every spk_* slot, and would the copy survive a promotion?"""
        leader = self.leader_member(subject)
        if leader is None:
            raise CheckFailed(f"scope {subject.scope} has no leader")
        standbys = [n for n in self.scope_members(subject)
                    if n.name != leader.name]
        if not standbys:
            raise CheckFailed(f"scope {subject.scope} has no standby")
        peers = self.peers(self._plan, subject)

        on_leader, _ = self.spock_slots(leader)
        checks.append(("leader_has_spock_slots", on_leader >= len(peers),
                       f"{leader.name}: {on_leader} for {len(peers)} peer(s)"))

        for standby in standbys:
            role, state = self.member_state(standby.name, leader)
            checks.append(("standby_streaming", state in SERVING_STATES,
                           f"{standby.name} role={role} state={state}"))

            def synced(standby=standby):
                count, _ = self.spock_slots(standby)
                return count >= len(peers) and count

            try:
                count, seconds = self.wait(
                    f"spk_* slots copied to {standby.name}", synced)
            except CheckFailed:
                count, _ = self.spock_slots(standby)
                checks.append(("standby_carries_spock_slots", False,
                               f"{standby.name}: {count} of {len(peers)}"))
                raise CheckFailed(
                    f"{standby.name} has {count} of {len(peers)} spk_* slots "
                    f"after {self.test_timeout}s: nothing has copied them, so "
                    f"a promotion would lose them"
                    + self.slot_sync_hint(standby))
            _, temporary = self.spock_slots(standby)
            checks.append(("standby_carries_spock_slots", True,
                           f"{standby.name}: {count} after {seconds}s"))
            checks.append(("standby_slots_permanent", temporary == 0,
                           f"{standby.name}: {temporary} temporary"))
            if temporary:
                raise CheckFailed(
                    f"{temporary} of {standby.name}'s spk_* slot copies are "
                    f"temporary: a temporary slot is dropped at promotion")

    def case_unplanned_failover(self, subject, checks):
        """Stop Patroni on the leader. The standby must be promoted, keep the
        slots, be re-targeted by the peers, hold every row, and replicate
        both ways; the old leader must rejoin once started again."""
        from aspects import cluster_services

        leader = self.leader_member(subject)
        if leader is None:
            raise CheckFailed(f"scope {subject.scope} has no leader")
        candidates = [n for n in self.scope_members(subject)
                      if n.name != leader.name]
        streaming = [c for c in candidates
                     if self.member_state(c.name, leader)[1] in SERVING_STATES]
        if not streaming:
            # Taking the leader down now would leave the scope with none.
            raise CheckFailed(
                f"no standby of {leader.name} is streaming "
                f"({', '.join(c.name for c in candidates) or 'none'}); "
                f"nothing was stopped")
        candidate = streaming[0]

        self.seed(subject, leader, checks)

        if self.log:
            self.log.info(f"      stopping Patroni on {leader.name}; expecting "
                          f"{candidate.name} to be promoted")
        ok, message = cluster_services.stop(
            self.executor_for(leader), self._plan, leader, component="patroni")
        if not ok:
            raise CheckFailed(f"could not stop Patroni on {leader.name}: "
                              f"{message}")
        try:
            seconds = self.wait_for_leader(subject, candidate, via=candidate)
            checks.append(("standby_promoted", True,
                           f"{candidate.name} in {seconds}s"))
            self.check_promoted(subject, candidate, checks)
        finally:
            # Whatever happened above, the old leader goes back into the
            # scope: the cluster must not be left a member short.
            cluster_services.start(
                self.executor_for(leader), self._plan, leader,
                component="patroni")
        seconds = self.wait_for_follower(leader, via=candidate)
        checks.append(("old_leader_rejoined", True,
                       f"{leader.name} as a replica in {seconds}s"))
        self.check_mesh(subject, candidate, "before", checks)

    def case_switchover_back(self, subject, checks):
        """A planned switchover to the Spock node's own member, which both
        exercises the clean path and restores the scope's original shape."""
        from aspects import patroni_management

        leader = self.leader_member(subject)
        if leader is None:
            raise CheckFailed(f"scope {subject.scope} has no leader")
        if leader.name == subject.name:
            # Nothing failed over before this case: switch to a standby and
            # back, so the planned path is still exercised.
            targets = [n for n in self.scope_members(subject)
                       if n.name != subject.name]
        else:
            targets = [subject]
        target = targets[0]
        role, state = self.member_state(target.name, leader)
        if state not in SERVING_STATES:
            raise CheckFailed(f"{target.name} is {state or 'absent'}, not "
                              f"streaming; no switchover attempted")

        phase = f"before-switchover-{target.name}"
        self.sql(leader, f"DELETE FROM {TABLE};")
        self.sql(leader, f"INSERT INTO {TABLE} (id, phase) "
                         f"SELECT g, {_lit(phase)} FROM generate_series(1, 100) g;")
        for peer in self.peers(self._plan, subject):
            self.wait_for_rows(peer, phase, 100,
                               f"rows before the switchover reaching {peer.name}")

        ok, output = patroni_management.switchover(
            self.executor_for(leader), leader, target.name, node_name=leader.name)
        if not ok:
            raise CheckFailed(f"patronictl switchover refused: "
                              f"{output.strip()[:140]}")
        seconds = self.wait_for_leader(subject, target, via=target)
        checks.append(("switched_over", True, f"to {target.name} in {seconds}s"))
        self.check_promoted(subject, target, checks)
        seconds = self.wait_for_follower(leader, via=target)
        checks.append(("former_leader_follows", True,
                       f"{leader.name} in {seconds}s"))
        self.check_mesh(subject, target, phase, checks)
        if target.name != subject.name:
            # Came in with the Spock node leading; hand it back.
            ok, output = patroni_management.switchover(
                self.executor_for(target), target, subject.name,
                node_name=target.name)
            if not ok:
                raise CheckFailed(f"switchover back to {subject.name} refused: "
                                  f"{output.strip()[:140]}")
            self.wait_for_leader(subject, subject, via=subject)
            self.repaired(subject, subject, checks)
        leader_now = self.leader_member(subject)
        checks.append(("scope_led_by_its_spock_node",
                       leader_now is not None and leader_now.name == subject.name,
                       f"leader is {leader_now.name if leader_now else 'none'}"))

    def case_cleanup(self, subject, checks):
        leader = self.leader_member(subject) or subject
        self.sql(leader, f"SELECT spock.replicate_ddl("
                         f"'DROP SCHEMA IF EXISTS {SCHEMA} CASCADE');")
        for peer in self.peers(self._plan, subject):
            def gone(peer=peer):
                value = self.scalar(
                    peer, f"SELECT count(*) FROM pg_namespace "
                          f"WHERE nspname = {_lit(SCHEMA)};", default="")
                return (value or "").strip() == "0"

            _, seconds = self.wait(f"schema dropped on {peer.name}", gone)
            checks.append(("schema_gone", True, f"{peer.name} in {seconds}s"))

    def artefacts(self, executor, node):
        return []

