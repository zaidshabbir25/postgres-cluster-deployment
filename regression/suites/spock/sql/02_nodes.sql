-- Does every node agree on who is in the cluster?
--
-- Asked of each peer in turn, because a mesh where n1 knows about n2 but n2
-- has forgotten n1 is broken in a way one node's view cannot show.
--
-- Where the peers are is not passed in: it is read from spock.node_interface,
-- which is what the deployment registered and what Spock's own procedures use.
-- A wrong DSN there is itself a fault worth finding, and this is the first
-- case that trips over it.
--
-- The aggregate lines are fixed, so they can have an expected file. Anything
-- naming a specific node is emitted only when that node is wrong: a healthy
-- cluster prints nothing extra, and a broken one prints lines that both fail
-- the comparison and say which node caused it.
SET client_min_messages = warning;

SELECT 'local_node_is_named|' ||
       ((spock.node_info()).node_name
        = current_setting('spock_regress.node'))::text;

SELECT 'local_sees_expected_nodes|' ||
       (count(*) = current_setting('spock_regress.node_count')::int)::text
FROM spock.node;

SELECT 'every_node_has_an_interface|' || (
    (SELECT count(*) FROM spock.node)
    = (SELECT count(DISTINCT if_nodeid) FROM spock.node_interface)
)::text;

SELECT 'peers_agree_on_membership|' || (count(*) = 0)::text
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT count(*)::int FROM spock.node') AS remote(seen int)
WHERE n.node_name <> current_setting('spock_regress.node')
  AND remote.seen <> current_setting('spock_regress.node_count')::int;

SELECT 'every_peer_knows_me|' || (count(*) = 0)::text
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT EXISTS (SELECT 1 FROM spock.node WHERE node_name = '
    || quote_literal(current_setting('spock_regress.node')) || ')')
    AS remote(knows boolean)
WHERE n.node_name <> current_setting('spock_regress.node')
  AND NOT remote.knows;

-- Detail, only when there is something to explain.
SELECT 'DISAGREES|' || n.node_name || '|sees ' || remote.seen || ' node(s)'
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT count(*)::int FROM spock.node') AS remote(seen int)
WHERE n.node_name <> current_setting('spock_regress.node')
  AND remote.seen <> current_setting('spock_regress.node_count')::int
ORDER BY 1;
