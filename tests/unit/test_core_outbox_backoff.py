import random

from chartwire.db.repo.outbox import MAX_BACKOFF_S, backoff_seconds


def test_backoff_doubles_with_jitter_and_caps():
    rng = random.Random(7)
    for attempts, base in ((1, 2), (3, 8), (8, 256), (9, MAX_BACKOFF_S), (20, MAX_BACKOFF_S)):
        value = backoff_seconds(attempts, rng)
        assert base * 0.8 <= value <= base * 1.2, (attempts, value)
