-- Does a large object written here reach every peer, change there, and go
-- away there?
--
-- The question lolor exists to answer. Create, update and unlink are
-- confirmed separately on every peer, so a failure says which step stopped
-- travelling rather than only that the nodes diverged.
--
-- Everything works inside the lolor_regress schema, which this case creates
-- and the last one drops. The object's OID is remembered in a table there,
-- so the cleanup case can remove it even if this case stops half way.
SET client_min_messages = warning;

CREATE SCHEMA IF NOT EXISTS lolor_regress;
CREATE TABLE IF NOT EXISTS lolor_regress.objects (loid oid PRIMARY KEY);

-- A helper, created in the suite's own schema and dropped with it. Polling for
-- a replicated change is the shape of every check in this file.
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

-- Created with its content in one call, through the standard API that lolor
-- has taken over.
SELECT lo_from_bytea(0, 'lolor health check payload'::bytea) AS loid \gset
INSERT INTO lolor_regress.objects VALUES (:loid) ON CONFLICT DO NOTHING;

SELECT 'oid_carries_this_node_id|' ||
       ((:loid::bigint & 15) = current_setting('lolor.node')::int)::text;

SELECT 'stored_in_lolor_not_catalog|' ||
       (EXISTS (SELECT 1 FROM lolor.pg_largeobject WHERE loid = :loid)
        AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_largeobject
                        WHERE loid = :loid))::text;

-- On each peer, read through that peer's own lo_get — proving the peer's lolor
-- serves the replicated object, not only that rows arrived in a table. CASE
-- so lo_get is not called while the object has yet to arrive.
SELECT 'content_reached_every_peer|' || lolor_regress.await(format(
    'SELECT CASE WHEN EXISTS (SELECT 1 FROM lolor.pg_largeobject WHERE loid = %s) '
    'THEN convert_from(lo_get(%s), ''UTF8'') = ''lolor health check payload'' '
    'ELSE false END', :loid, :loid))::text;

-- Overwrite six bytes in place; the peers must see the change, not a copy.
-- Through a counted subquery: lo_put returns void, and a bare SELECT of it
-- would print an empty row that no expected line matches.
SELECT 'updated_locally|' || (count(*) = 1)::text
FROM (SELECT lo_put(:loid, 6, 'HEALTH'::bytea)) AS done;

SELECT 'update_reached_every_peer|' || lolor_regress.await(format(
    'SELECT CASE WHEN EXISTS (SELECT 1 FROM lolor.pg_largeobject WHERE loid = %s) '
    'THEN convert_from(lo_get(%s), ''UTF8'') = ''lolor HEALTH check payload'' '
    'ELSE false END', :loid, :loid))::text;

SELECT 'unlinked_locally|' || (lo_unlink(:loid) = 1)::text;

SELECT 'unlink_reached_every_peer|' || lolor_regress.await(format(
    'SELECT NOT EXISTS (SELECT 1 FROM lolor.pg_largeobject WHERE loid = %s)',
    :loid))::text;
