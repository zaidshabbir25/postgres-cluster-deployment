-- Do rows written here reach every peer?
--
-- The question the cluster exists to answer. Insert, update and delete are
-- confirmed separately on every peer, so a failure says which operation
-- stopped travelling rather than only that the data diverged.
SET client_min_messages = warning;

-- A helper, created in the suite's own schema and dropped with it. Polling for
-- a replicated row is the shape of every check in this file.
CREATE OR REPLACE FUNCTION spock_regress.await(want_sql text, seconds int DEFAULT 60)
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
        WHERE n.node_name <> current_setting('spock_regress.node')
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

INSERT INTO spock_regress.basic (id, label)
VALUES (1, 'one'), (2, 'two'), (3, 'three');

SELECT 'insert_reached_every_peer|' || spock_regress.await(
    'SELECT count(*) = 3 FROM spock_regress.basic')::text;

UPDATE spock_regress.basic SET label = 'two-updated' WHERE id = 2;

SELECT 'update_reached_every_peer|' || spock_regress.await(
    'SELECT EXISTS (SELECT 1 FROM spock_regress.basic '
    'WHERE id = 2 AND label = ''two-updated'')')::text;

DELETE FROM spock_regress.basic WHERE id = 3;

SELECT 'delete_reached_every_peer|' || spock_regress.await(
    'SELECT NOT EXISTS (SELECT 1 FROM spock_regress.basic WHERE id = 3)')::text;

SELECT 'local_row_count_is_two|' || (count(*) = 2)::text
FROM spock_regress.basic;
