"""Turning Presidio's spans into handles, and remembering which handle meant who.

This is the app's own logic, deliberately NOT in the masking service. Presidio has no
notion of entity instance numbering, and the service that does the detection is the
wrong place to invent one: numbering only means anything across a conversation, and a
shared masking service holding per-session PII is a liability rather than a feature.
So the service returns spans and this module decides what to call each entity.

Three styles:

    typed       <PERSON>                 what NeMo produces
    indexed     <PERSON_1>, <PERSON_2>   distinct values stay distinguishable
    surrogate   "Peter Jackson"          a stable fake value of the same shape

GRANULARITY: WHY NAMES ARE SPLIT INTO WORDS BY DEFAULT

Presidio reports occurrences, not identities -- there is no coreference resolution
anywhere in the stack. So "Sara Banderby" and a later bare "Sara" arrive as two spans
with nothing linking them, and naming whole spans gives them two handles:

    span   Sara Banderby met Erwan Granger. Sara liked Erwan.
        -> <PERSON_1> met <PERSON_2>. <PERSON_3> liked Erwan.

Naming each WORD instead makes the shared word share its handle, which is as close to
linking the two mentions as you get without entity resolution:

    word   -> <NAME_1> <NAME_2> met <NAME_3> <NAME_4>. <NAME_1> liked <NAME_3>

A word of a name is not a person, so split handles are labelled NAME rather than
PERSON: "<PERSON_1> <PERSON_2>" for one person reads as two.

The cost is the opposite error. Two different people who share a first name share that
handle, so the mapping asserts they are one person: "Sara Banderby" and "Sara Lee" both
hold <NAME_1> for "Sara". Span-level over-counts, word-level under-counts, and
neither is identity resolution. Word-level is the default because partial references
are far more common in prose than collisions on a first name -- set
MASK_GRANULARITY=span to swap the trade.

Splitting applies only to entity types where a word is a meaningful unit (PERSON by
default). Splitting an email address or a personnummer would be nonsense.

Deduplication is also case-insensitive, so a later "sara" reuses the handle issued for
"Sara". The mapping shows the spelling seen first.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from typing import Literal

from cachetools import TTLCache

log = logging.getLogger(__name__)

Style = Literal["typed", "indexed", "surrogate"]
STYLES: tuple[Style, ...] = ("typed", "indexed", "surrogate")

Granularity = Literal["span", "word"]
GRANULARITIES: tuple[Granularity, ...] = ("span", "word")

# Entity types whose spans are split into words. Only types where a word is a
# meaningful unit belong here.
DEFAULT_WORD_ENTITIES: tuple[str, ...] = ("PERSON",)

# How a split type is LABELLED in its handle. A word of a person's name is not a
# person, so `<PERSON_1> <PERSON_2>` for "Sara Banderby" reads as two people; NAME says
# what the handle actually stands for. Only the rendering changes -- the entity type
# stays PERSON everywhere it matters (the mapping, the surrogate generator lookup).
DEFAULT_WORD_LABELS: dict[str, str] = {"PERSON": "NAME"}

_WORD = re.compile(r"\S+")

DEFAULT_TEMPLATES: dict[str, str] = {
    "typed": "<{entity_type}>",
    "indexed": "<{entity_type}_{index}>",
    "surrogate": "<{entity_type}_{index}>",
}

# Faker generators per entity type. Anything not listed falls back to the template,
# which is the honest answer: inventing a plausible value for an entity we have no
# generator for produces something neither masked nor real.
_SURROGATES: dict[str, str] = {
    "PERSON": "name",
    "EMAIL_ADDRESS": "email",
    "PHONE_NUMBER": "phone_number",
    "LOCATION": "city",
    "CREDIT_CARD": "credit_card_number",
    "IBAN_CODE": "iban",
    "IP_ADDRESS": "ipv4",
    "URL": "url",
    "DATE_TIME": "date",
}


@dataclass(frozen=True)
class Span:
    """One entity as /analyze reported it: an offset into the submitted text."""

    start: int
    end: int
    entity_type: str
    text: str


@dataclass(frozen=True)
class Replacement:
    handle: str
    value: str
    entity_type: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"handle": self.handle, "value": self.value, "entity_type": self.entity_type}


@dataclass(frozen=True)
class Masked:
    text: str
    mapping: tuple[Replacement, ...]
    changed: bool


def split_words(spans: list[Span], types: tuple[str, ...] = DEFAULT_WORD_ENTITIES) -> list[Span]:
    """One span per word, for the listed entity types. Others pass through."""
    out: list[Span] = []
    for span in spans:
        if span.entity_type not in types:
            out.append(span)
            continue
        for match in _WORD.finditer(span.text):
            out.append(
                Span(
                    start=span.start + match.start(),
                    end=span.start + match.end(),
                    entity_type=span.entity_type,
                    text=match.group(),
                )
            )
    return out


def _key(entity_type: str, value: str) -> tuple[str, str]:
    """Dedupe key: casefolded, so a later "sara" reuses the handle for "Sara"."""
    return (entity_type, value.casefold())


class Registry:
    """Per-session `(entity_type, value) -> handle`, so a handle means one thing.

    Without this, numbering restarts every request: "Dana" alone is <PERSON_1> and so
    is "Mike" alone in the next turn, and a session-wide mapping built from those is
    simply wrong. Keyed by session so counters continue across turns.

    This holds PII in memory for the TTL. That is a real cost, accepted here because
    this process already handles the user's original text -- it is the application.
    The masking service stays stateless.
    """

    def __init__(self, maxsize: int = 512, ttl: int = 3600) -> None:
        # Bounded and expiring, so an abandoned session cannot pin PII forever.
        self._sessions: TTLCache[str, dict] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.Lock()

    @staticmethod
    def _fresh() -> dict:
        return {"issued": {}, "counts": {}}

    def _state(self, session: str) -> dict:
        state = self._sessions.get(session)
        if state is None:
            state = self._fresh()
            self._sessions[session] = state
        return state

    def assign(
        self,
        session: str | None,
        spans: list[Span],
        style: Style,
        template: str,
        labels: dict[str, str] | None = None,
    ) -> dict[tuple[str, str], Replacement]:
        """Handles for these spans, reusing any this session already issued."""
        with self._lock:
            # No session id: fall back to per-request numbering rather than sharing a
            # registry between callers, which would leak handles across users.
            state = self._state(session) if session else self._fresh()
            issued: dict[tuple[str, str], Replacement] = state["issued"]
            counts: dict[str, int] = state["counts"]
            for span in spans:
                key = _key(span.entity_type, span.text)
                if key in issued:
                    continue
                counts[span.entity_type] = counts.get(span.entity_type, 0) + 1
                issued[key] = Replacement(
                    handle=_handle(
                        span.entity_type,
                        counts[span.entity_type],
                        span.text,
                        style,
                        template,
                        (labels or {}).get(span.entity_type),
                    ),
                    # The spelling seen first, which is what the mapping shows.
                    value=span.text,
                    entity_type=span.entity_type,
                )
            return dict(issued)

    def forget(self, session: str | None) -> None:
        if not session:
            return
        with self._lock:
            self._sessions.pop(session, None)


def _handle(
    entity_type: str, index: int, value: str, style: Style, template: str, label: str | None = None
) -> str:
    """`label` renders in place of the entity type; the type itself still drives the
    surrogate generator, so a split PERSON word still becomes a fake name."""
    rendered = label or entity_type
    if style == "surrogate":
        surrogate = _surrogate(entity_type, value)
        if surrogate is not None:
            return surrogate
        if template == DEFAULT_TEMPLATES["surrogate"]:
            return DEFAULT_TEMPLATES["indexed"].format(entity_type=rendered, index=index)
    return template.format(entity_type=rendered, index=index)


def _surrogate(entity_type: str, value: str) -> str | None:
    """A fake value of the same shape, stable for a given original value.

    Seeding from the value rather than a counter means the same person reads the same
    way in every request. It also makes the fake value a deterministic function of the
    real one: a pseudonym, not an anonymisation. Anyone who can run this with the same
    seed can confirm a guess. Fine for keeping a model's output natural; not a
    substitute for masking in a published record, where it is also indistinguishable
    from unmasked data.
    """
    generator = _SURROGATES.get(entity_type)
    if generator is None:
        return None
    from faker import Faker  # imported lazily: only this style needs it

    faker = Faker("en_US")
    faker.seed_instance(int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big"))
    return str(getattr(faker, generator)())


def validate_template(template: str) -> str:
    """Raise ValueError unless the template renders."""
    if not template:
        raise ValueError("mask template must not be empty")
    try:
        template.format(entity_type="PERSON", index=1)
    except (IndexError, KeyError, ValueError) as exc:
        raise ValueError(
            f"mask template may only reference {{entity_type}} and {{index}}; "
            f"could not render {template!r}: {exc}"
        ) from exc
    return template


def apply(
    text: str,
    spans: list[Span],
    issued: dict[tuple[str, str], Replacement],
    style: Style,
    template: str,
) -> Masked:
    """Rewrite `text`, replacing each span with its handle."""
    if not spans:
        return Masked(text=text, mapping=(), changed=False)

    # Back to front, so each replacement leaves the offsets before it untouched.
    masked = text
    for span in sorted(spans, key=lambda s: s.start, reverse=True):
        handle = issued[_key(span.entity_type, span.text)].handle
        masked = masked[: span.start] + handle + masked[span.end :]

    # Only the handles used in THIS text, so a turn's mapping describes that turn.
    used = {_key(s.entity_type, s.text) for s in spans}
    mapping = tuple(issued[key] for key in used)
    # A mapping is only meaningful when every handle is distinct. A template can
    # collide even with {index} in it: "PERSON_{index}" has no {entity_type}, so the
    # first PERSON and the first EMAIL_ADDRESS both become "PERSON_1".
    distinct = len({r.handle for r in mapping}) == len(mapping)
    meaningful = (style == "surrogate" or "{index}" in template) and distinct
    if not meaningful and style != "typed":
        log.warning("mask template %r produces colliding handles; no mapping returned", template)
    return Masked(
        text=masked,
        mapping=tuple(sorted(mapping, key=lambda r: r.handle)) if meaningful else (),
        changed=masked != text,
    )
