-- Is each node's outbound replication actually connected?
--
-- A subscription can read "replicating" on the subscriber while the provider's
-- slot is inactive and WAL piles up behind it. This asks the provider's side
-- of the same question, which is where that shows.
SET client_min_messages = warning;

SELECT 'logical_slots_present|' || (count(*) >= current_setting('spock_regress.node_count')::int - 1)::text
FROM pg_replication_slots WHERE slot_type = 'logical';

SELECT 'all_logical_slots_active|' || (count(*) = 0)::text
FROM pg_replication_slots WHERE slot_type = 'logical' AND NOT active;

-- Retained WAL is the symptom that costs disk. A slot a few segments behind is
-- normal; one holding gigabytes is a stuck subscriber.
SELECT 'no_slot_retaining_excessive_wal|' || (count(*) = 0)::text
FROM pg_replication_slots
WHERE slot_type = 'logical'
  AND pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 1024 * 1024 * 1024;

SELECT 'walsenders_running|' || (count(*) >= current_setting('spock_regress.node_count')::int - 1)::text
FROM pg_stat_replication;
