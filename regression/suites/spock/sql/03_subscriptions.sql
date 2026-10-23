-- Is every subscription actually replicating, on every node?
--
-- spock.sub_show_status() is the authoritative view: spock.subscription
-- records intent, while this says whether the apply worker is connected. A
-- cross-wired mesh of N nodes carries N-1 subscriptions on each of them.
--
-- As in the previous case, anything naming a subscription is printed only when
-- that subscription is down.
SET client_min_messages = warning;

SELECT 'local_subscription_count|' ||
       (count(*) = current_setting('spock_regress.node_count')::int - 1)::text
FROM spock.sub_show_status();

SELECT 'local_all_replicating|' || (count(*) = 0)::text
FROM spock.sub_show_status() WHERE status <> 'replicating';

SELECT 'peers_all_replicating|' || (count(*) = 0)::text
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT count(*)::int FROM spock.sub_show_status() '
    'WHERE status <> ''replicating''') AS remote(down int)
WHERE n.node_name <> current_setting('spock_regress.node')
  AND remote.down > 0;

SELECT 'DOWN|local|' || subscription_name || '|' || status
FROM spock.sub_show_status()
WHERE status <> 'replicating'
ORDER BY 1;

SELECT 'DOWN|' || n.node_name || '|' || remote.down || ' subscription(s)'
FROM spock.node n
JOIN spock.node_interface i ON i.if_nodeid = n.node_id
CROSS JOIN LATERAL dblink(i.if_dsn,
    'SELECT count(*)::int FROM spock.sub_show_status() '
    'WHERE status <> ''replicating''') AS remote(down int)
WHERE n.node_name <> current_setting('spock_regress.node')
  AND remote.down > 0
ORDER BY 1;
