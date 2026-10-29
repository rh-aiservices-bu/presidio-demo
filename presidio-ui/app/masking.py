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

log = logging.getLogger(__name__)


class MaskError(RuntimeError):
    """Masking could not be completed. Callers decide whether that is fatal."""


class MaskBlocked(RuntimeError):
    """A detect-style rail refused the text outright."""


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
