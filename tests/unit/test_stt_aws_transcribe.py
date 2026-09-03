"""Amazon Transcribe event mapping with fake SDK objects — no network, no SDK required."""

from __future__ import annotations

from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from chartwire.stt.aws_transcribe import AwsTranscribeStream, AwsTranscribeStreaming, map_results
from chartwire.stt.base import Chunk, Final, Partial, SessionInfo


def item(speaker: str | None, confidence: float | None) -> NS:
    return NS(speaker=speaker, confidence=confidence)


def result(
    text: str, *, partial: bool, start: float = 0.0, end: float = 1.0, items: list | None = None
) -> NS:
    return NS(
        is_partial=partial, start_time=start, end_time=end, alternatives=[NS(transcript=text, items=items)]
    )


def test_partial_and_final_mapping():
    events = map_results(
        [
            result("안녕하", partial=True),
            result(
                "안녕하세요 어떻게 지내셨어요",
                partial=False,
                start=0.12,
                end=1.5,
                items=[item("spk_0", 0.9), item("spk_0", 0.8)],
            ),
        ],
        from_seq=3,
        seq=8,
    )
    assert events[0] == Partial(from_seq=3, text="안녕하")
    final = events[1]
    assert isinstance(final, Final)
    assert (final.seq_start, final.seq_end, final.speaker) == (3, 8, "clinician")
    assert (final.t_start_ms, final.t_end_ms, final.confidence) == (120, 1500, 0.85)


def test_speaker_majority_custom_labels_and_unknown():
    items = [item("spk_1", 0.9), item("spk_1", 0.9), item("spk_0", 0.9)]
    (final,) = map_results([result("잠이 안 와요", partial=False, items=items)], from_seq=1, seq=1)
    assert isinstance(final, Final) and final.speaker == "patient"
    (final,) = map_results(
        [result("x", partial=False, items=items)], from_seq=1, seq=1, speaker_labels={"spk_1": "clinician"}
    )
    assert isinstance(final, Final) and final.speaker == "clinician"
    (final,) = map_results([result("x", partial=False, items=[item(None, None)])], from_seq=1, seq=1)
    assert isinstance(final, Final) and final.speaker == "unknown" and final.confidence == 0.0


def test_empty_results_and_blank_transcripts_are_skipped():
    assert (
        map_results([NS(is_partial=False, start_time=0, end_time=1, alternatives=[])], from_seq=1, seq=1)
        == []
    )
    assert map_results([result("   ", partial=True), result("", partial=False)], from_seq=1, seq=1) == []


class FakeInput:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.ended = False

    async def send_audio_event(self, *, audio_chunk: bytes) -> None:
        self.sent.append(audio_chunk)

    async def end_stream(self) -> None:
        self.ended = True


class FakeOutput:
    def __init__(self, events: list) -> None:
        self._events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class FakeClient:
    def __init__(self, events: list) -> None:
        self.kwargs: dict = {}
        self.stream = NS(input_stream=FakeInput(), output_stream=FakeOutput(events))

    async def start_stream_transcription(self, **kwargs):
        self.kwargs = kwargs
        return self.stream


def transcript_event(*results: NS) -> NS:
    return NS(transcript=NS(results=list(results)))


async def test_stream_feeds_audio_and_drains_mapped_events():
    events = [
        transcript_event(result("안녕", partial=True)),
        NS(other=True),  # non-transcript event is ignored
        transcript_event(result("안녕하세요", partial=False, items=[item("spk_0", 0.95)])),
        transcript_event(result("잠이", partial=True)),
    ]
    client = FakeClient(events)
    adapter = AwsTranscribeStreaming("ap-northeast-2", client=client)
    info = SessionInfo(uuid4(), uuid4(), None, 200)
    stream = await adapter.open(info)
    assert isinstance(stream, AwsTranscribeStream)
    assert client.kwargs["language_code"] == "ko-KR" and client.kwargs["show_speaker_label"] is True

    got = []
    for seq in (1, 2, 3):
        got += await stream.feed(Chunk(info.session_id, seq, (seq - 1) * 200, 0, b"pcm%d" % seq))
    got += await stream.flush()
    assert client.stream.input_stream.sent == [b"pcm1", b"pcm2", b"pcm3"]
    assert client.stream.input_stream.ended
    partials = [e for e in got if isinstance(e, Partial)]
    finals = [e for e in got if isinstance(e, Final)]
    assert [p.text for p in partials] == ["안녕", "잠이"]
    assert len(finals) == 1 and finals[0].text == "안녕하세요" and finals[0].seq_start == 1
    # the utterance after a final starts at the chunk in which the final was drained
    assert partials[1].from_seq == finals[0].seq_end


def test_open_without_sdk_and_without_client_is_a_clear_error(monkeypatch: pytest.MonkeyPatch):
    import chartwire.stt.aws_transcribe as mod

    monkeypatch.setattr(mod, "TranscribeStreamingClient", None)
    with pytest.raises(RuntimeError):
        AwsTranscribeStreaming("ap-northeast-2")
