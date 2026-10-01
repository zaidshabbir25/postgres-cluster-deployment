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

## Removing a node

`zodremove.sql` is the inverse operation, and `node remove` calls it. It
installs `spock.remove_node(target_node_name, target_node_dsn, verbose)`, which
unwinds the mesh in the order that leaves nothing behind: subscriptions first —
which take their replication slots with them — then replication sets, then the
node registration on every peer. Dropping only one side of a subscription pair
leaves a slot retaining WAL on a healthy node forever, which is the failure this
ordering exists to prevent.

It must run **on the node being removed**: its first act is to compare
`spock.node_info()` with the name it was given and refuse if they differ.
Everything it touches on the peers it reaches through dblink. That is the mirror
of `spock.add_node`, which also runs on the node that is joining.

The file is read from the same branch as `zodan.sql` — the source-build
checkout when it is on that commit, otherwise the branch on GitHub — because
the procedures read Spock's catalogs directly and a revision from the wrong
branch fails at runtime rather than at load time.

| Spock major | Bundled fallback     |
|-------------|----------------------|
| `spock50`   | `zodremove-504.sql`  |
| `spock60`   | none — fetched from the branch |

The bundled copy is only a fallback for a host with no network and no checkout.
There is no spock60 one on purpose: a 5.0.x removal script reads catalog columns
spock60 renamed, so an air-gapped spock60 host is told to supply the script
rather than given one that will fail halfway through a removal. The mapping is
`ZODREMOVE_BY_SPOCK_MAJOR` in `aspects/spock_management.py`.

## Updating

Copy a newer revision in from the upstream repository and add it to
`ZODAN_BY_SPOCK_MAJOR`. Keep the old file: a cluster deployed with one revision
should keep using it, because the procedures and the catalog expectations move
together.
