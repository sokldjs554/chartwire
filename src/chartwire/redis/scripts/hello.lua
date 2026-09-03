-- hello.lua — one atomic step per recorder hello (spec §5, §6.1).
--
-- KEYS[1] sess:{sid}          HASH  hot ingest state
-- KEYS[2] ctl:{sid}           PUB/SUB control channel
-- KEYS[3] sess:{sid}:chunks   STREAM api -> stt-worker
-- KEYS[4] stt:active          SET   sessions the stt-workers must serve
-- ARGV[1] node_id, ARGV[2] conn_id, ARGV[3] updated_at (iso), ARGV[4] sid, ARGV[5] consumer group
--
-- Returns {epoch, HGETALL(sess)}. The epoch is incremented first so the PUBLISH carries the
-- number the previous connection must compare against (it ignores epochs <= its own).
local epoch = redis.call('HINCRBY', KEYS[1], 'epoch', 1)
redis.call('HSET', KEYS[1], 'node', ARGV[1], 'conn', ARGV[2], 'updated_at', ARGV[3])
redis.call('PERSIST', KEYS[1])
redis.call('PUBLISH', KEYS[2], cjson.encode({t = 'superseded', epoch = epoch}))
-- consumer group: MKSTREAM creates the stream; BUSYGROUP (already exists) is not an error here
redis.pcall('XGROUP', 'CREATE', KEYS[3], ARGV[5], '0', 'MKSTREAM')
redis.call('PERSIST', KEYS[3])
redis.call('SADD', KEYS[4], ARGV[4])
return {epoch, redis.call('HGETALL', KEYS[1])}
