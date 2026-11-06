-- Put the cluster back as it was.
--
-- Run whatever happened above: a large object this suite created is
-- replicated, so one left behind by a case that stopped early is on every
-- node. Its OID was recorded in lolor_regress.objects for exactly this
-- reason. The schema is then dropped; if DDL replication carried it to the
-- peers, the drop travels the same way.
SET client_min_messages = warning;

-- The table and helper exist only if 03 ran; make sure of both so this case
-- can run on its own.
CREATE SCHEMA IF NOT EXISTS lolor_regress;
CREATE TABLE IF NOT EXISTS lolor_regress.objects (loid oid PRIMARY KEY);
CREATE OR REPLACE FUNCTION lolor_regress.await(want_sql text, seconds int DEFAULT 60)
RETURNS boolean LANGUAGE plpgsql AS $fn$
DECLARE
    deadline timestamptz := clock_timestamp() + make_interval(secs => seconds);
    pending  int;
BEGIN
    LOOP
        SELECT count(*) INTO pending
        FROM spock.node n
        JOIN spock.node_interface i ON i.if_nodeid = n.node_id
        CROSS JOIN LATERAL dblink(i.if_dsn, want_sql) AS remote(satisfied boolean)
        WHERE n.node_name <> current_setting('lolor_regress.node')
          AND NOT remote.satisfied;

        IF pending = 0 THEN
            RETURN true;
        END IF;
        IF clock_timestamp() > deadline THEN
            RETURN false;
        END IF;
        PERFORM pg_sleep(0.25);
    END LOOP;
END;
$fn$;

DO $$
DECLARE
    leftover oid;
BEGIN
    FOR leftover IN
        SELECT o.loid FROM lolor_regress.objects o
        WHERE EXISTS (SELECT 1 FROM lolor.pg_largeobject WHERE loid = o.loid)
    LOOP
        PERFORM lo_unlink(leftover);
    END LOOP;
END $$;

SELECT 'no_suite_objects_remain_locally|' || (count(*) = 0)::text
FROM lolor_regress.objects o
WHERE EXISTS (SELECT 1 FROM lolor.pg_largeobject WHERE loid = o.loid);

SELECT 'no_suite_objects_remain_on_peers|' || lolor_regress.await(format(
    'SELECT NOT EXISTS (SELECT 1 FROM lolor.pg_largeobject '
    'WHERE loid = ANY (%L::oid[]))',
    (SELECT coalesce(array_agg(loid), '{}') FROM lolor_regress.objects)))::text;

DROP SCHEMA lolor_regress CASCADE;

SELECT 'schema_gone|' || (to_regnamespace('lolor_regress') IS NULL)::text;
