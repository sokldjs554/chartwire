"""Amazon Transcribe Streaming adapter (spec §3.1): real event-mapping code, import-guarded.

The SDK (``amazon-transcribe``, optional extra ``aws``) is only needed to *open* a stream; the
mapping from Transcribe ``TranscriptEvent`` results to :class:`Partial`/:class:`Final` is plain
Python over duck-typed objects, so it is unit-tested offline with fake events (no network, no
credentials). Transcribe is never exercised on the build box (§0).

Speaker attribution: Transcribe returns ``spk_0, spk_1, …`` labels when ``show_speaker_label`` is
on. The consultation-room convention adopted here is *first voice = clinician* (the clinician
greets first, see §10.2); a different mapping is passed via ``speaker_labels``. Unknown labels and
items without a label map to ``'unknown'``.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from chartwire.stt.base import Chunk, Final, Partial, SessionInfo, Speaker, SttEvent

try:  # pragma: no cover - exercised only where the optional extra is installed
    from amazon_transcribe.client import TranscribeStreamingClient
except ImportError:  # pragma: no cover
    TranscribeStreamingClient = None

DEFAULT_SPEAKER_LABELS: Mapping[str, Speaker] = {"spk_0": "clinician", "spk_1": "patient"}
LANGUAGE_CODE = "ko-KR"
SAMPLE_RATE_HZ = 16_000


class _Item(Protocol):
    speaker: str | None
    confidence: float | None


class _Alternative(Protocol):
    transcript: str | None
    items: list[Any] | None


class _Result(Protocol):
    is_partial: bool
    start_time: float
    end_time: float
    alternatives: list[Any] | None


def map_results(
    results: Iterable[_Result],
    *,
    from_seq: int,
    seq: int,
    speaker_labels: Mapping[str, Speaker] = DEFAULT_SPEAKER_LABELS,
) -> list[SttEvent]:
    """Map the ``results`` of one ``TranscriptEvent`` to STT events.

    ``from_seq`` is the first chunk of the utterance in progress, ``seq`` the chunk most recently
    fed (both are unknown to Transcribe, which reasons in seconds of audio).
    """
    out: list[SttEvent] = []
    for result in results:
        alts = result.alternatives or []
        if not alts:
            continue
        alt = alts[0]
        text = (alt.transcript or "").strip()
        if not text:
            continue
        if result.is_partial:
            out.append(Partial(from_seq=from_seq, text=text))
            continue
        items = alt.items or []
        out.append(
            Final(
                seq_start=from_seq,
                seq_end=seq,
                speaker=_majority_speaker(items, speaker_labels),
                t_start_ms=round(result.start_time * 1000),
                t_end_ms=round(result.end_time * 1000),
                text=text,
                confidence=_mean_confidence(items),
            )
        )
    return out


def _majority_speaker(items: Iterable[_Item], labels: Mapping[str, Speaker]) -> Speaker:
    votes: Counter[Speaker] = Counter()
    for item in items:
        label = getattr(item, "speaker", None)
        if label is not None:
            votes[labels.get(label, "unknown")] += 1
    if not votes:
        return "unknown"
    return votes.most_common(1)[0][0]


def _mean_confidence(items: Iterable[_Item]) -> float:
    scores = [float(c) for c in (getattr(item, "confidence", None) for item in items) if c is not None]
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 4)


class AwsTranscribeStream:
    """Pumps Transcribe output events into a queue; ``feed``/``flush`` drain it with seq context."""

    def __init__(self, stream: Any, speaker_labels: Mapping[str, Speaker]) -> None:
        self._stream = stream
        self._labels = speaker_labels
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._from_seq: int | None = None
        self._last_seq = 0
        self._pump = asyncio.create_task(self._pump_events())

    async def _pump_events(self) -> None:
        async for event in self._stream.output_stream:
            transcript = getattr(event, "transcript", None)
            if transcript is not None:
                await self._queue.put(transcript.results or [])

    async def feed(self, chunk: Chunk) -> list[SttEvent]:
        await self._stream.input_stream.send_audio_event(audio_chunk=chunk.payload)
        self._last_seq = chunk.seq
        if self._from_seq is None:
            self._from_seq = chunk.seq
        await asyncio.sleep(0)  # let the pump task pick up events that already arrived
        return self._drain()

    async def flush(self) -> list[SttEvent]:
        await self._stream.input_stream.end_stream()
        await self._pump
        return self._drain()

    def _drain(self) -> list[SttEvent]:
        events: list[SttEvent] = []
        while not self._queue.empty():
            results = self._queue.get_nowait()
            from_seq = self._from_seq if self._from_seq is not None else self._last_seq
            mapped = map_results(results, from_seq=from_seq, seq=self._last_seq, speaker_labels=self._labels)
            for ev in mapped:
                if isinstance(ev, Final):
                    self._from_seq = self._last_seq  # the next utterance may start inside this chunk
            events.extend(mapped)
        return events


class AwsTranscribeStreaming:
    """``SttAdapter`` over ``TranscribeStreamingClient``; ``client`` is injectable for tests."""

    def __init__(
        self,
        region: str,
        *,
        client: Any | None = None,
        speaker_labels: Mapping[str, Speaker] = DEFAULT_SPEAKER_LABELS,
    ) -> None:
        if client is None:
            if TranscribeStreamingClient is None:
                raise RuntimeError(
                    "amazon-transcribe 패키지가 설치되어 있지 않습니다 (extra: chartwire[aws])"
                )
            client = TranscribeStreamingClient(region=region)
        self._client = client
        self._labels = speaker_labels

    async def open(self, session: SessionInfo) -> AwsTranscribeStream:
        stream = await self._client.start_stream_transcription(
            language_code=LANGUAGE_CODE,
            media_sample_rate_hz=SAMPLE_RATE_HZ,
            media_encoding="pcm",
            show_speaker_label=True,
            session_id=str(session.session_id),
        )
        return AwsTranscribeStream(stream, self._labels)
