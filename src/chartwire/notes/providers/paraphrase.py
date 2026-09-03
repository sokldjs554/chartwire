"""Paraphrasing mock provider — legitimate rewrites for the false-rejection eval (spec §9.2).

Rewrites the ``S`` statements of the extractive draft the way a careful model would, while keeping
every evidence quote verbatim. Each transform is recorded on ``last_applied`` so the eval reports
the verifier's false-rejection rate *per transform*; this is the one non-trivial grounding number
available offline, and the residual causes are listed rather than tuned away.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Literal

from chartwire.notes.extractive import ExtractiveProvider
from chartwire.notes.korean import transform_ending
from chartwire.notes.providers.base import NoteProvider, Stopwatch, raw_from_draft
from chartwire.notes.schema import DraftContext, NoteDraftOut, RawDraft, Statement, parse_draft

Transform = Literal[
    "ending_plain",
    "ending_formal",
    "particle_swap",
    "synonym",
    "merge",
    "number_word",
    "honorific_drop",
]
TRANSFORMS: tuple[Transform, ...] = (
    "ending_plain",
    "ending_formal",
    "particle_swap",
    "synonym",
    "merge",
    "number_word",
    "honorific_drop",
)

SYNONYMS: tuple[tuple[str, str], ...] = (
    ("잠을 못 자요", "수면 곤란"),
    ("입맛이 없어요", "식욕 저하"),
    ("가슴이 두근거려요", "심계항진 호소"),
    ("기운이 없어요", "무기력감"),
    ("걱정이 많아요", "과도한 걱정"),
)
_PARTICLE_SWAP = {"은": "이", "는": "가", "이": "은", "가": "는"}
# "where safe": only the sentence-initial word, where 은/는/이/가 is a topic/subject marker on a
# noun (``잠은 …``, ``기분이 …``) and never a verb ending (``받은 적이``).
_PARTICLE_RE = re.compile(r"^([가-힣]+)([은는이가]) ")
_NUMBER_WORDS = {
    1: "한",
    2: "두",
    3: "세",
    4: "네",
    5: "다섯",
    6: "여섯",
    7: "일곱",
    8: "여덟",
    9: "아홉",
    10: "열",
}
_NATIVE_UNITS = "시간|달|주|번|회|잔|병|알|정"
_DIGIT_UNIT_RE = re.compile(rf"\b(\d+)\s*({_NATIVE_UNITS})")
_WORD_UNIT_RE = re.compile(
    rf"({'|'.join(sorted(_NUMBER_WORDS.values(), key=len, reverse=True))}) ({_NATIVE_UNITS})"
)
_MAX_TEXT = 200


@dataclass(frozen=True)
class AppliedTransform:
    transform: Transform
    index: int
    """Index of the rewritten statement in the returned draft."""


def paraphrase(draft: NoteDraftOut, rng: random.Random) -> tuple[NoteDraftOut, tuple[AppliedTransform, ...]]:
    """Rewrite every ``S`` statement with one randomly chosen applicable transform."""
    out: list[Statement] = []
    applied: list[AppliedTransform] = []
    statements = list(draft.statements)
    i = 0
    while i < len(statements):
        st = statements[i]
        nxt = statements[i + 1] if i + 1 < len(statements) else None
        if st.section != "S":
            out.append(st)
            i += 1
            continue
        options = [(t, r) for t in TRANSFORMS if (r := _rewrite(t, st, nxt)) is not None]
        if not options:
            out.append(st)
            i += 1
            continue
        transform, rewritten = rng.choice(options)
        out.append(rewritten)
        applied.append(AppliedTransform(transform=transform, index=len(out) - 1))
        i += 2 if transform == "merge" else 1
    return NoteDraftOut(statements=out, abstain=draft.abstain, abstain_reason=draft.abstain_reason), tuple(
        applied
    )


def _rewrite(transform: Transform, st: Statement, nxt: Statement | None) -> Statement | None:
    quote = st.evidence[0].quote
    text: str | None
    if transform == "ending_plain":
        text = transform_ending(quote, "plain")
    elif transform == "ending_formal":
        text = transform_ending(quote, "formal")
    elif transform == "particle_swap":
        plain = transform_ending(quote, "plain")
        text = (
            _PARTICLE_RE.sub(lambda m: m.group(1) + _PARTICLE_SWAP[m.group(2)] + " ", plain)
            if plain
            else None
        )
        text = None if text == plain else text
    elif transform == "synonym":
        text = _synonym(quote)
    elif transform == "merge":
        return _merge(st, nxt)
    elif transform == "number_word":
        plain = transform_ending(quote, "plain") or st.text
        text = _swap_number_words(plain)
    else:
        text = quote[:-1] if transform_ending(quote, "plain") is not None and quote.endswith("요") else None
    if text is None or not 1 <= len(text) <= _MAX_TEXT:
        return None
    return st.model_copy(update={"text": text})


def _synonym(quote: str) -> str | None:
    for key, value in SYNONYMS:
        if key in quote:
            text = quote.replace(key, value, 1)
            return transform_ending(text, "plain") or text
    return None


def _merge(a: Statement, b: Statement | None) -> Statement | None:
    if b is None or b.section != "S" or len(a.evidence) + len(b.evidence) > 4:
        return None
    text = f"{transform_ending(a.evidence[0].quote, 'plain') or a.text}, {transform_ending(b.evidence[0].quote, 'plain') or b.text}"
    if len(text) > _MAX_TEXT:
        return None
    return a.model_copy(update={"text": text, "evidence": [*a.evidence, *b.evidence]})


def _swap_number_words(text: str) -> str | None:
    m = _DIGIT_UNIT_RE.search(text)
    if m is not None and int(m.group(1)) in _NUMBER_WORDS:
        return text[: m.start()] + f"{_NUMBER_WORDS[int(m.group(1))]} {m.group(2)}" + text[m.end() :]
    m = _WORD_UNIT_RE.search(text)
    if m is not None:
        digit = next(d for d, w in _NUMBER_WORDS.items() if w == m.group(1))
        return text[: m.start()] + f"{digit}{m.group(2)}" + text[m.end() :]
    return None


class ParaphrasingMockProvider:
    def __init__(self, seed: int, base: NoteProvider | None = None) -> None:
        self.base: NoteProvider = base if base is not None else ExtractiveProvider()
        self.rng = random.Random(seed)
        self.name = f"paraphrase({self.base.name})"
        self.last_applied: tuple[AppliedTransform, ...] = ()

    async def draft(self, ctx: DraftContext) -> RawDraft:
        watch = Stopwatch()
        base = parse_draft((await self.base.draft(ctx)).text)
        draft, self.last_applied = paraphrase(base, self.rng)
        return raw_from_draft(draft, provider=self.name, latency_ms=watch.elapsed_ms)
