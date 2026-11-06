-- Is Spock actually installed and usable on the node we are connected to?
--
-- Every later case assumes this, so it is first and it is cheap. dblink is
-- checked too: every case after this one reads the peers through it rather
-- than reconnecting, which is what lets the whole suite run in one session
-- against a cluster that is already up.
SET client_min_messages = warning;

SELECT 'spock_installed|' || (count(*) = 1)::text
FROM pg_extension WHERE extname = 'spock';

SELECT 'dblink_installed|' || (count(*) = 1)::text
FROM pg_extension WHERE extname = 'dblink';

SELECT 'version_reported|' || (spock.spock_version() <> '')::text;

SELECT 'version_matches_extension|' || (spock.spock_version() = extversion)::text
FROM pg_extension WHERE extname = 'spock';

-- The entry points the rest of the suite calls. Checked by name rather than
-- by signature: the argument lists have changed between Spock releases, and a
-- test that fails because a default was added would be reporting on itself.
SELECT 'has_' || want || '|' || EXISTS (
           SELECT 1 FROM pg_proc p
           JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'spock' AND p.proname = want
       )::text
FROM unnest(ARRAY['node_info', 'sub_show_status', 'repset_add_table',
                  'replicate_ddl', 'node_add_interface']) AS want
ORDER BY 1;
