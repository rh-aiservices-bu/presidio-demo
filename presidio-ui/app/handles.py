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

WHAT THE DEDUPLICATION IS, AND IS NOT

Two spans get the same handle when their text is byte-identical. That is string
equality, not identity: Presidio reports occurrences, and nothing in this stack
resolves which occurrences are the same person. "Dana Whitfield" and a later "Dana"
are two handles, as are "Dana Whitfield" and "dana whitfield". Masking stays correct;
the mapping just reads as more people than were mentioned. Collapsing them would need
entity resolution, a different and much harder problem than detection.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Literal

from cachetools import TTLCache

log = logging.getLogger(__name__)

Style = Literal["typed", "indexed", "surrogate"]
STYLES: tuple[Style, ...] = ("typed", "indexed", "surrogate")

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

    def _state(self, session: str) -> dict:
        state = self._sessions.get(session)
        if state is None:
            state = {"handles": {}, "counts": {}}
            self._sessions[session] = state
        return state

    def assign(self, session: str | None, spans: list[Span], style: Style, template: str) -> dict:
        """Handles for these spans, reusing any this session already issued."""
        with self._lock:
            # No session id: fall back to per-request numbering rather than sharing a
            # registry between callers, which would leak handles across users.
            state = self._state(session) if session else {"handles": {}, "counts": {}}
            handles: dict[tuple[str, str], str] = state["handles"]
            counts: dict[str, int] = state["counts"]
            for span in spans:
                key = (span.entity_type, span.text)
                if key in handles:
                    continue
                counts[span.entity_type] = counts.get(span.entity_type, 0) + 1
                handles[key] = _handle(
                    span.entity_type, counts[span.entity_type], span.text, style, template
                )
            return dict(handles)

    def forget(self, session: str | None) -> None:
        if not session:
            return
        with self._lock:
            self._sessions.pop(session, None)


def _handle(entity_type: str, index: int, value: str, style: Style, template: str) -> str:
    if style == "surrogate":
        surrogate = _surrogate(entity_type, value)
        if surrogate is not None:
            return surrogate
        if template == DEFAULT_TEMPLATES["surrogate"]:
            return DEFAULT_TEMPLATES["indexed"].format(entity_type=entity_type, index=index)
    return template.format(entity_type=entity_type, index=index)


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
    handles: dict[tuple[str, str], str],
    style: Style,
    template: str,
) -> Masked:
    """Rewrite `text`, replacing each span with its handle."""
    if not spans:
        return Masked(text=text, mapping=(), changed=False)

    # Back to front, so each replacement leaves the offsets before it untouched.
    masked = text
    for span in sorted(spans, key=lambda s: s.start, reverse=True):
        handle = handles[(span.entity_type, span.text)]
        masked = masked[: span.start] + handle + masked[span.end :]

    # Only the handles used in THIS text, so a turn's mapping describes that turn.
    used = {(s.entity_type, s.text) for s in spans}
    mapping = tuple(
        Replacement(handle=handles[key], value=key[1], entity_type=key[0]) for key in used
    )
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
