-- Does DDL reach the peers?
--
-- Everything from here on works inside the spock_regress schema, which the
-- suite creates, owns, and drops again in the last case. Nothing touches a
-- table the cluster's own users put there.
--
-- Settings rather than psql variables for the node name: psql does not
-- interpolate :name inside a dollar-quoted body, and these cases need the same
-- value in both places.
SET client_min_messages = warning;

SELECT spock.replicate_ddl('CREATE SCHEMA IF NOT EXISTS spock_regress');

SELECT spock.replicate_ddl($ddl$
    CREATE TABLE spock_regress.basic (
        id integer PRIMARY KEY,
        label text,
        touched timestamptz DEFAULT now()
    )
$ddl$);

SELECT 'table_exists_locally|' ||
       (to_regclass('spock_regress.basic') IS NOT NULL)::text;

-- DDL travels as its own replicated statement rather than through a repset, so
-- it can arrive a moment later than a row would.
DO $$
DECLARE
    deadline timestamptz := clock_timestamp() + interval '60 seconds';
    pending  int;
BEGIN
    LOOP
        SELECT count(*) INTO pending
        FROM spock.node n
        JOIN spock.node_interface i ON i.if_nodeid = n.node_id
        CROSS JOIN LATERAL dblink(i.if_dsn,
            'SELECT to_regclass(''spock_regress.basic'') IS NULL')
            AS remote(missing boolean)
        WHERE n.node_name <> current_setting('spock_regress.node')
          AND remote.missing;

        EXIT WHEN pending = 0;
        IF clock_timestamp() > deadline THEN
            RAISE EXCEPTION 'the replicated table never reached % peer(s)',
                            pending;
        END IF;
        PERFORM pg_sleep(0.25);
    END LOOP;
END $$;

SELECT 'table_replicated_to_every_peer|true';

-- Into the default replication set, so the rows in the next case travel.
SELECT 'added_to_repset|' ||
       spock.repset_add_table('default', 'spock_regress.basic')::text;
