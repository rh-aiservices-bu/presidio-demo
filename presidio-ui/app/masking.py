"""Client for the NeMo Guardrails masking service.

Uses POST /v1/checks, which is the only one of the three endpoints that returns the
text *after* rails processing:

    {"status": "passed|modified|blocked", "content": "<masked text>"}

/v1/guardrail/checks returns a verdict with no content field at all, and
/v1/chat/completions runs the dialog rails, which fail against this lab's
Bedrock-backed model. See ../README.md.

Config selection: the handler in this build reads body.guardrails.config_ids. The
inline body.guardrails.config field is declared in the schema but read by no code,
so per-request entity selection is not possible -- entity changes go through the
ConfigMap (see entities.py).

Both an async and a sync client are provided on purpose. The request path is async;
the MLflow span processor is a synchronous callback and cannot await.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

import httpx

from .handles import Span

log = logging.getLogger(__name__)


class MaskError(RuntimeError):
    """Masking could not be completed. Callers decide whether that is fatal."""


class MaskBlocked(RuntimeError):
    """A detect-style rail refused the text outright."""


class MaskingUnmapped(RuntimeError):
    """The endpoint cannot return spans -- i.e. it is a NeMo server.

    Not an error condition: it is how the UI discovers which engine it is talking to.
    """


@dataclass(frozen=True)
class MaskResult:
    status: Literal["passed", "modified"]
    content: str

    @property
    def changed(self) -> bool:
        return self.status == "modified"


# A pre-filter must never produce a FALSE NEGATIVE, because a skipped string is
# exported verbatim. An earlier version of this tried to guess what Presidio would
# flag -- "a digit, an @, or two adjacent capitalised words" -- and leaked
# "Hi my name is Robert" straight into a trace: no digit, no @, and no two ADJACENT
# capitalised words, so it was never even sent for masking. Presidio's NER sees a
# PERSON there from sentence context; a regex cannot.
#
# So the only safe filter is one that excludes strings Presidio provably cannot flag.
# Every one of the 20 supported entities requires alphanumeric content, so text with
# none (empty, whitespace, pure punctuation) is safe to skip and nothing else is.
# Everything else goes to NeMo, through the LRU cache in tracing.py.
_HAS_ALNUM = re.compile(r"[0-9A-Za-z]")


def worth_checking(text: str) -> bool:
    """True unless the text provably cannot contain any supported entity."""
    return bool(text) and bool(_HAS_ALNUM.search(text))


def _payload(model: str, config_id: str, text: str) -> dict:
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
    }
    if config_id:
        # config_ids (plural) is what the /v1/checks handler actually reads.
        body["guardrails"] = {"config_ids": [config_id]}
    return body


def _parse(data: object) -> MaskResult:
    if not isinstance(data, dict):
        raise MaskError("masking service returned a non-object body")
    status = data.get("status")
    if status == "blocked":
        raise MaskBlocked(str(data.get("rail") or "a guardrail refused the text"))
    if status not in {"passed", "modified"}:
        raise MaskError(f"unexpected status from masking service: {status!r}")
    content = data.get("content")
    if not isinstance(content, str):
        raise MaskError("masking service returned no content field")
    return MaskResult(status=status, content=content)


def _parse_spans(data: object) -> tuple[Span, ...]:
    if not isinstance(data, dict) or not isinstance(data.get("spans"), list):
        raise MaskError("masking service returned no spans field")
    spans: list[Span] = []
    for row in data["spans"]:
        if not isinstance(row, dict):
            continue
        try:
            spans.append(
                Span(
                    start=int(row["start"]),
                    end=int(row["end"]),
                    entity_type=str(row["entity_type"]),
                    text=str(row["text"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MaskError(f"masking service returned a malformed span: {exc}") from exc
    return tuple(spans)


async def analyze_text(
    client: httpx.AsyncClient, nemo_url: str, text: str
) -> tuple[Span, ...]:
    """Presidio's spans for `text`, via POST /analyze.

    Raises MaskingUnmapped when the endpoint does not exist, which is how a NeMo
    server answers. Callers fall back to mask_text and get plain placeholders.
    """
    if not text:
        return ()
    try:
        resp = await client.post(f"{nemo_url}/analyze", json={"text": text})
        if resp.status_code in (404, 405):
            raise MaskingUnmapped(f"{nemo_url} has no /analyze endpoint")
        resp.raise_for_status()
        data = resp.json()
    except MaskingUnmapped:
        raise
    except httpx.HTTPStatusError as exc:
        raise MaskError(f"masking service returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise MaskError(f"masking service unreachable: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise MaskError("masking service returned invalid JSON") from exc
    return _parse_spans(data)


async def mask_text(
    client: httpx.AsyncClient, nemo_url: str, model: str, config_id: str, text: str
) -> MaskResult:
    """Mask one string. Raises MaskError on any failure -- never returns the input."""
    if not text:
        return MaskResult(status="passed", content=text)
    try:
        resp = await client.post(
            f"{nemo_url}/v1/checks", json=_payload(model, config_id, text)
        )
        resp.raise_for_status()
        data = resp.json()
    except MaskBlocked:
        raise
    except httpx.HTTPStatusError as exc:
        raise MaskError(f"masking service returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise MaskError(f"masking service unreachable: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise MaskError("masking service returned invalid JSON") from exc
    return _parse(data)


def mask_text_sync(
    client: httpx.Client, nemo_url: str, model: str, config_id: str, text: str
) -> MaskResult:
    """Synchronous twin of mask_text, for the MLflow span processor."""
    if not text:
        return MaskResult(status="passed", content=text)
    try:
        resp = client.post(f"{nemo_url}/v1/checks", json=_payload(model, config_id, text))
        resp.raise_for_status()
        data = resp.json()
    except MaskBlocked:
        raise
    except httpx.HTTPStatusError as exc:
        raise MaskError(f"masking service returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise MaskError(f"masking service unreachable: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise MaskError("masking service returned invalid JSON") from exc
    return _parse(data)
