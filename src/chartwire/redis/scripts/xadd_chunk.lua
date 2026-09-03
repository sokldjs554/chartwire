-- xadd_chunk.lua — epoch-fenced XADD of one chunk's metadata (spec §5, §6.1).
--
-- KEYS[1] sess:{sid}          HASH
-- KEYS[2] sess:{sid}:chunks   STREAM
-- ARGV[1] epoch of the sending connection, ARGV[2] MAXLEN (approximate), ARGV[3] updated_at (iso),
-- ARGV[4..] stream field/value pairs (seq,key,len,off,fl,ep,ts — no audio bytes)
--
-- Returns  1 = appended · 0 = stale epoch (a newer hello superseded this connection; no XADD)
--         -1 = hot state missing (Redis lost sess:{sid}; the shell fails closed and rehydrates)
local current = redis.call('HGET', KEYS[1], 'epoch')
if not current then
  return -1
end
if tonumber(current) ~= tonumber(ARGV[1]) then
  return 0
end
local args = {KEYS[2], 'MAXLEN', '~', ARGV[2], '*'}
for i = 4, #ARGV do
  args[#args + 1] = ARGV[i]
end
redis.call('XADD', unpack(args))
redis.call('HSET', KEYS[1], 'updated_at', ARGV[3])
return 1
