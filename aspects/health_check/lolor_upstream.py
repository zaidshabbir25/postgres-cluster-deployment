#!/usr/bin/env python3
"""lolor's own regression suite, fetched fresh at the cluster's ref.

Upstream's `make installcheck`, which despite the name starts a scratch
instance: lolor's Makefile sets `REGRESS_OPTS = --temp-instance=./tmp_check
--temp-config=regress.conf`, because the drop guard is an object_access_hook
that only works preloaded and the suite creates cluster-global roles it does
not want on a shared server. The instance preloads the *installed* lolor
library, so the verdict is about the build the nodes run, in isolation.

Every run clones the ref anew — the tag a source build named, or the
component's default branch for a packaged one — so the cases are whatever
upstream has today for that version. The in-tree `lolor` suite covers what
this cannot: large objects actually travelling between the deployed nodes.
Asking for `lolor` runs both.

The TAP tests (`TAP_TESTS = 1`) are not run: they need IPC::Run and a
`prove` setup the hosts do not have, and they too build their own instance.
"""

import shlex

from aspects.health_check.runner import PgRegressSuite


class LolorUpstreamSuite(PgRegressSuite):
    name = "lolor-upstream"
    repo = "https://github.com/pgEdge/lolor.git"
    scope = ("a scratch instance on the host, with the lolor library the "
             "nodes run")

    def missing_requirements(self, executor, plan, node):
        missing = super().missing_requirements(executor, plan, node)
        _, pkglibdir = executor.try_run(
            f"{shlex.quote(node.bin_dir)}/pg_config --pkglibdir 2>/dev/null",
            node=node.name)
        module = f"{pkglibdir.strip() or '/unknown'}/lolor.so"
        found, _ = executor.try_run(f"test -f {shlex.quote(module)}",
                                    node=node.name)
        if not found:
            missing.append(f"the installed lolor library at {module}")
        return missing
