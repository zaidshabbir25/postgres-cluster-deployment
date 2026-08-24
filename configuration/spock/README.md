# zodan cross-wiring procedures

These SQL files install `spock.add_node` and the procedures it calls. They are
vendored from the pgEdge [`pgedge-pep-test`](https://github.com/pgEdge/pgedge-pep-test)
repository (`config/spock/`), which is where they are maintained.

## Why the deployment uses them

Raw Spock gives you `node_create` and `sub_create`. Wiring an N-node mesh with
those means creating N·(N−1) subscriptions by hand and getting the sync-event
ordering right so a node joining a *live* cluster does not miss writes that
land while it is catching up.

`spock.add_node(src_node, src_dsn, new_node, new_dsn, verbose)` does that in one
call. It discovers every node already registered with `src_node`, then wires the
newcomer to all of them in both directions across 13 phases — prerequisite
checks, disabled subscriptions, slot creation, sync-event handshakes, slot
advance, then enabling everything. So adding the Nth node is always a single
call against node 1, no matter how large the cluster already is.

## Picking a file

The Spock major version decides which revision applies, because spock60 renamed
catalog columns that these procedures read (`remote_lsn` became
`remote_commit_lsn`). Using the wrong one fails at runtime, not at load time.

| Spock major | File            |
|-------------|-----------------|
| `spock50`   | `zodan-511.sql` |
| `spock60`   | `zodan-600.sql` |

The mapping lives in `aspects/spock_management.py` (`ZODAN_BY_SPOCK_MAJOR`) and
is selected by `--spock-major`. Override it for one run with
`--zodan-sql zodan-600.sql`, or pin it per PostgreSQL version with
`ZODAN_SQL_SPOCK<major>` in `configuration/config<major>.env`.

## Other files

`zodremove-504.sql` provides the inverse operation — removing a node from a
cross-wired cluster. The deployment does not call it; it is here for manual use
when you want to detach a node without tearing the whole cluster down.

## Updating

Copy a newer revision in from the upstream repository and add it to
`ZODAN_BY_SPOCK_MAJOR`. Keep the old file: a cluster deployed with one revision
should keep using it, because the procedures and the catalog expectations move
together.
