from uuid import UUID

from chartwire.redis import keys
from chartwire.redis.client import with_db

SID = UUID("018f0000-0000-7000-8000-000000000001")


def test_key_names_match_spec_table():
    assert keys.sess(SID) == f"sess:{SID}"
    assert keys.sess_chunks(SID) == f"sess:{SID}:chunks"
    assert keys.sess_events(SID) == f"sess:{SID}:events"
    assert keys.sess_viewers(SID) == f"sess:{SID}:viewers"
    assert keys.ctl(SID) == f"ctl:{SID}"
    assert keys.stt_owner(SID) == f"stt:owner:{SID}"
    assert keys.ticket("abc") == "ticket:abc"
    assert keys.ratelimit("t", "p", "rest", 12) == "rl:t:p:rest:12"
    assert keys.idempotency("t", "k") == "idem:t:k"
    assert keys.node("api-1") == "node:api-1"
    assert (keys.STT_ACTIVE, keys.STT_LAG, keys.ALERTS_SLA) == ("stt:active", "stt:lag", "alerts:sla")
    assert (keys.KEYS_INVALIDATE, keys.OUTBOX_WAKE) == ("keys:invalidate", "outbox:wake")
    assert keys.sess_pattern(SID).endswith("*") and keys.sess(SID).startswith(keys.sess_pattern(SID)[:-1])
    assert keys.CHUNK_FIELDS == ("seq", "key", "len", "off", "fl", "ep", "ts") and keys.END_MARKER == {
        "end": "1"
    }


def test_with_db_rewrites_index():
    assert with_db("redis://localhost:6379/0", 3) == "redis://localhost:6379/3"
    assert with_db("redis://localhost:6379", 3) == "redis://localhost:6379/3"
    assert with_db("rediss://u:p@host:6380/2", 8) == "rediss://u:p@host:6380/8"
