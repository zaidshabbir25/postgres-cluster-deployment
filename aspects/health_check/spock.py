#!/usr/bin/env python3
"""Spock's health check, written for a cluster that is already running.

Upstream's `tests/regress` cannot be used here, and the reason is worth
recording. Those cases build the thing they test: `init.sql` creates the nodes
and the subscription, after creating passwordless roles `super` and `nonsuper`
that only work under the `trust` pg_hba upstream ships for its throwaway
instance. Against a deployed cluster every Spock connection as `super` fails
authentication, `init` fails, and all thirty-eight cases after it fail with it.
Making them pass would mean lowering a running cluster's authentication and
leaving a superuser behind. Worse, upstream's `check` target is
`check: install regresscheck`: run on a cluster host, it would install that
checkout's spock.so over the one every node is running.

So the cases here are our own, and they ask the opposite question: given a
cluster that is already cross-wired and already serving, does replication work?
Nothing is created, initialised or torn down — no instance, no node, no
subscription, no role, no database, and no change to pg_hba. The only objects
that appear are a `spock_regress` schema and one table in it, dropped again by
the last case.

Three things make that possible, and InTreeSuite in runner.py implements them:

  * peers are found through `spock.node_interface`, which is where the
    deployment registered them and what Spock's own procedures read. A wrong
    DSN there is a real fault, and case 02 is the first to trip over it.
  * peers are read through dblink in one session rather than by reconnecting,
    so nothing depends on psql's handling of a conninfo in a variable, and
    nothing needs a password the suite does not already have.
  * output is tuples-only and unaligned, and every case prints fixed `name|true`
    lines. Detail that names a node is printed only when that node is wrong, so
    a healthy cluster matches a fixed expected file and a broken one produces a
    diff that says which node and why.
"""

from pathlib import Path

from aspects.health_check.runner import InTreeSuite

SUITE_DIR = Path(__file__).resolve().parent / "suites" / "spock"
SQL_DIR = SUITE_DIR / "sql"
EXPECTED_DIR = SUITE_DIR / "expected"

#: The schema the cases work in. Created by 05 and dropped by 08.
SCHEMA = "spock_regress"


def available_tests():
    return SpockSuite.available_tests()


class SpockSuite(InTreeSuite):
    name = "spock"
    suite_dir = SUITE_DIR
    settings_prefix = "spock_regress"
