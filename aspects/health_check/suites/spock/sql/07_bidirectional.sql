-- Does a write on a peer come back to us?
--
-- Multi-master is the point: every node accepts writes. The previous case
-- proved one direction, and a mesh wired only one way passes it. This writes
-- on each peer, through dblink, and waits for the row here.
SET client_min_messages = warning;

DO $$
DECLARE
    peer     record;
    deadline timestamptz;
    arrived  boolean;
    seed     int := 100;
BEGIN
    FOR peer IN
        SELECT n.node_name, i.if_dsn
        FROM spock.node n
        JOIN spock.node_interface i ON i.if_nodeid = n.node_id
        WHERE n.node_name <> current_setting('spock_regress.node')
        ORDER BY n.node_name
    LOOP
        seed := seed + 1;
        PERFORM dblink_exec(peer.if_dsn,
            format('INSERT INTO spock_regress.basic (id, label) VALUES (%s, %L)',
                   seed, 'from-' || peer.node_name));

        deadline := clock_timestamp() + interval '60 seconds';
        LOOP
            SELECT EXISTS (SELECT 1 FROM spock_regress.basic WHERE id = seed)
              INTO arrived;
            EXIT WHEN arrived;
            IF clock_timestamp() > deadline THEN
                RAISE EXCEPTION 'a row written on % never arrived here',
                                peer.node_name;
            END IF;
            PERFORM pg_sleep(0.25);
        END LOOP;
    END LOOP;
END $$;

SELECT 'every_peer_write_came_back|true';
