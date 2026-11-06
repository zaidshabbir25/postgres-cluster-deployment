-- Does every node have a lolor identity of its own, and are lolor's tables
-- replicated everywhere?
--
-- Two nodes sharing a lolor.node value generate colliding OIDs and nothing
-- detects it until two objects collide, so the ids are read from every peer
-- and compared here. And a large object only travels if lolor's storage
-- tables are in a replication set on the node that writes it — which has to
-- be every node, since every node is a writer.
--
-- The aggregate lines are fixed, so they can have an expected file. Anything
-- naming a specific node is emitted only when that node is wrong.
SET client_min_messages = warning;

SELECT 'node_ids_are_distinct|' ||
       (count(DISTINCT ids.node_id) = count(*))::text
FROM (
    SELECT current_setting('lolor.node')::int AS node_id
    UNION ALL
    SELECT remote.node_id
    FROM spock.node n
    JOIN spock.node_interface i ON i.if_nodeid = n.node_id
    CROSS JOIN LATERAL dblink(i.if_dsn,
        'SELECT coalesce(current_setting(''lolor.node'', true), ''0'')::int')
        AS remote(node_id int)
    WHERE n.node_name <> current_setting('lolor_regress.node')
) AS ids;

SELECT 'every_peer_has_a_valid_id|' || (count(*) = 0)::text
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT coalesce(current_setting(''lolor.node'', true), ''0'')::int')
    AS remote(node_id int)
WHERE n.node_name <> current_setting('lolor_regress.node')
  AND remote.node_id NOT BETWEEN 1 AND 15;

SELECT 'storage_tables_replicated_locally|' || (count(*) = 2)::text
FROM spock.tables
WHERE nspname = 'lolor'
  AND relname IN ('pg_largeobject', 'pg_largeobject_metadata')
  AND set_name IS NOT NULL;

SELECT 'storage_tables_replicated_on_every_peer|' || (count(*) = 0)::text
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT count(*)::int FROM spock.tables WHERE nspname = ''lolor'' '
    'AND relname IN (''pg_largeobject'', ''pg_largeobject_metadata'') '
    'AND set_name IS NOT NULL')
    AS remote(replicated int)
WHERE n.node_name <> current_setting('lolor_regress.node')
  AND remote.replicated < 2;

-- Detail, only when there is something to explain.
SELECT 'DISAGREES|' || n.node_name || '|lolor.node=' || remote.node_id
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT coalesce(current_setting(''lolor.node'', true), ''0'')::int')
    AS remote(node_id int)
WHERE n.node_name <> current_setting('lolor_regress.node')
  AND (remote.node_id NOT BETWEEN 1 AND 15
       OR remote.node_id = current_setting('lolor.node')::int)
ORDER BY 1;

SELECT 'NOT_REPLICATED|' || n.node_name || '|' || remote.replicated
       || ' of 2 lolor tables in a replication set'
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT count(*)::int FROM spock.tables WHERE nspname = ''lolor'' '
    'AND relname IN (''pg_largeobject'', ''pg_largeobject_metadata'') '
    'AND set_name IS NOT NULL')
    AS remote(replicated int)
WHERE n.node_name <> current_setting('lolor_regress.node')
  AND remote.replicated < 2
ORDER BY 1;
