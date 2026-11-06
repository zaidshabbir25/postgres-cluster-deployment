-- Is lolor actually installed, loaded and identified on the node we are
-- connected to?
--
-- Every later case assumes this, so it is first and it is cheap. Three
-- things have to be true at once, and each fails differently when it is
-- not: the extension created in this database, the library in
-- shared_preload_libraries (lolor refuses to be loaded on demand), and
-- lolor.node set to a value in 1..15 — the four low bits of every large
-- object OID it generates.
SET client_min_messages = warning;

SELECT 'lolor_installed|' || (count(*) = 1)::text
FROM pg_extension WHERE extname = 'lolor';

SELECT 'dblink_installed|' || (count(*) = 1)::text
FROM pg_extension WHERE extname = 'dblink';

SELECT 'lolor_preloaded|' ||
       ('lolor' = ANY (string_to_array(
            replace(current_setting('shared_preload_libraries'), ' ', ''),
            ',')))::text;

SELECT 'node_id_is_set|' ||
       (coalesce(current_setting('lolor.node', true), '0')::int
        BETWEEN 1 AND 15)::text;

-- lolor renames the native pg_catalog.lo_* functions to lo_*_orig and puts
-- its own in their place; the renamed original is the proof the swap
-- happened in this database.
SELECT 'lo_functions_are_lolors|' || EXISTS (
           SELECT 1 FROM pg_proc p
           JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'pg_catalog' AND p.proname = 'lo_creat_orig'
       )::text;

SELECT 'storage_tables_exist|' ||
       (to_regclass('lolor.pg_largeobject') IS NOT NULL
        AND to_regclass('lolor.pg_largeobject_metadata') IS NOT NULL)::text;
