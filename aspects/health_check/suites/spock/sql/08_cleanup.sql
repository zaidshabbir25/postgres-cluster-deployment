-- Put the cluster back as it was.
--
-- Run whatever happened above: these objects are replicated, so leaving them
-- behind leaves them on every node, and the next run would find its own table
-- already there. DROP ... CASCADE through replicate_ddl removes it everywhere
-- in one statement.
SET client_min_messages = warning;

SELECT 'removed_from_repset|' || coalesce(
    (SELECT spock.repset_remove_table('default', 'spock_regress.basic')::text
     WHERE to_regclass('spock_regress.basic') IS NOT NULL),
    'true');

SELECT spock.replicate_ddl('DROP SCHEMA IF EXISTS spock_regress CASCADE');

SELECT 'schema_gone_locally|' ||
       (to_regclass('spock_regress.basic') IS NULL)::text;

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
            'SELECT to_regclass(''spock_regress.basic'') IS NOT NULL')
            AS remote(still_there boolean)
        WHERE n.node_name <> current_setting('spock_regress.node')
          AND remote.still_there;

        EXIT WHEN pending = 0;
        IF clock_timestamp() > deadline THEN
            RAISE EXCEPTION 'the schema is still on % peer(s)', pending;
        END IF;
        PERFORM pg_sleep(0.25);
    END LOOP;
END $$;

SELECT 'schema_gone_everywhere|true';
