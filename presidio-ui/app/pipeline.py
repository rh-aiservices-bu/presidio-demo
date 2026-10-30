"""The three masking modes.

    mode    sent to the LLM   recorded in MLflow             NeMo calls
    none    original          original                       0
    model   masked            masked (same as what was sent)  1
    logs    ORIGINAL          masked user text and reply     1-2

`model` masks on the REQUEST path, before the call -- that is the whole feature, and
nothing trace-specific is involved because the span simply records what was sent.

`logs` masks on the TRACE path, in the span processor in tracing.py. This app records
the original values and the processor rewrites them at span end. That indirection is
deliberate: it is what makes the mode cover spans this app does not author, which is
what will matter as soon as tools are added.

Failure behaviour differs by mode because what masking protects differs:
  * `model` fails closed -- a NeMo outage aborts the turn before the model sees anything.
  * `logs` fails the trace, not the turn -- the model is meant to receive the original
    here, so aborting would protect nothing. The span is scrubbed instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from openai import AsyncOpenAI

from .config import Settings
from .llm import LlmError, LlmUsage, build_messages, stream_completion
from .handles import (
    DEFAULT_TEMPLATES,
    DEFAULT_WORD_LABELS,
    Masked,
    Registry,
    Replacement,
    Style,
    apply as apply_handles,
    split_words,
)
from .masking import (
    MaskBlocked,
    MaskError,
    MaskingUnmapped,
    analyze_text,
    mask_text,
)
from .schemas import Mode
from .tracing import SCRUBBED, clear_intent, experiment_id, register_intent

log = logging.getLogger(__name__)


@dataclass
class Deps:
    settings: Settings
    http: httpx.AsyncClient
    oai: AsyncOpenAI
    # Per-session handle registry, so <PERSON_1> means one person for a whole
    # conversation rather than restarting at 1 every request.
    handles: Registry = field(default_factory=Registry)
    # None until the first masked turn tells us whether the configured engine serves
    # /analyze. See _mask_with_handles.
    mapping_supported: bool | None = None


@dataclass
class Prepared:
    """Decided before any streaming starts, so failures can still become a 503."""

    sent_text: str
    logged_user: str
    mask_status: str
    warnings: list[str] = field(default_factory=list)
    # Only populated in `model` mode, and only by an engine that returns one. It is
    # sent to the browser and NEVER to a span -- see the note in stream_turn.
    mapping: tuple[Replacement, ...] = ()


@dataclass
class Streamed:
    text: str = ""
    usage: LlmUsage | None = None
    error: str | None = None


async def _mask(d: Deps, text: str) -> str:
    s = d.settings
    result = await mask_text(d.http, s.nemo_url, s.llm_model, s.nemo_config_id, text)
    return result.content


async def _mask_with_handles(d: Deps, text: str, session: str | None) -> Masked:
    """Mask using Presidio's spans, naming each entity ourselves.

    The masking service only detects; the numbering, the handle format and the mapping
    are this app's job, which is why they are in handles.py. A NeMo server has no
    /analyze endpoint and answers 404 -- not an error, just an engine that cannot do
    this -- so we fall back to its own placeholder masking and show no mapping. The
    answer is remembered, so only the first turn pays for the probe.
    """
    s = d.settings
    if d.mapping_supported is not False:
        try:
            spans = list(await analyze_text(d.http, s.nemo_url, text))
            d.mapping_supported = True
            labels: dict[str, str] = {}
            if s.mask_granularity == "word":
                # One handle per word, so a partial later mention reuses it. Split
                # handles are labelled NAME: a word of a name is not a person.
                spans = split_words(spans, s.mask_word_entities)
                labels = {
                    t: DEFAULT_WORD_LABELS.get(t, t) for t in s.mask_word_entities
                }
            style: Style = s.mask_style
            template = s.mask_token or DEFAULT_TEMPLATES[style]
            issued = d.handles.assign(session, spans, style, template, labels)
            return apply_handles(text, spans, issued, style, template)
        except MaskingUnmapped:
            log.info("%s has no /analyze endpoint; continuing without mappings", s.nemo_url)
            d.mapping_supported = False

    content = await _mask(d, text)
    return Masked(text=content, mapping=(), changed=content != text)


async def prepare(
    d: Deps, text: str, mode: Mode, session: str | None = None
) -> Prepared:
    """Run the pre-LLM masking. Raises MaskError/MaskBlocked so the route can 503."""
    if mode == "none":
        return Prepared(sent_text=text, logged_user=text, mask_status="off")

    if mode == "model":
        # Fail closed: if this raises, nothing reaches the model and nothing is traced.
        result = await _mask_with_handles(d, text, session)
        masked = result.text
        return Prepared(
            sent_text=masked,
            logged_user=masked,
            mask_status="masked" if masked != text else "no-pii",
            mapping=result.mapping,
        )

    # logs: the model gets the original. We still mask now rather than later, because
    # it gives the UI its preview and warms the cache the span processor will hit.
    try:
        masked = await _mask(d, text)
    except MaskBlocked:
        raise
    except MaskError as exc:
        log.warning("logs-mode preview masking failed, trace will be scrubbed: %s", exc)
        return Prepared(
            sent_text=text,
            logged_user=SCRUBBED,
            mask_status="degraded",
            warnings=["Masking service unavailable - the trace was scrubbed wholesale."],
        )
    return Prepared(
        sent_text=text,
        logged_user=masked,
        mask_status="masked" if masked != text else "no-pii",
    )


def trace_url(s: Settings, trace_id: str | None = None) -> str | None:
    """Deep link into MLflow. `workspace` is mandatory, and a trace is selected by query
    param -- experiments/:id/traces/:traceId is not a route."""
    exp = experiment_id()
    if not (s.mlflow_browser_url and exp):
        return None
    base = f"{s.mlflow_browser_url}/experiments/{exp}/traces?workspace={s.mlflow_workspace}"
    return f"{base}&selectedEvaluationId={trace_id}" if trace_id else base


async def _pump(d: Deps, sent_text: str, out: Streamed) -> AsyncIterator[str]:
    """Stream the model's reply, accumulating it into `out` as we go."""
    messages = build_messages(d.settings, sent_text)
    try:
        async for delta, chunk_usage in stream_completion(d.oai, d.settings, messages):
            if chunk_usage is not None:
                out.usage = chunk_usage
            if delta:
                out.text += delta
                yield delta
    except LlmError as exc:
        out.error = str(exc)


async def stream_turn(
    d: Deps, text: str, mode: Mode, prepared: Prepared
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE-shaped events: {'type': 'delta'|'meta'|'error', ...}."""
    s = d.settings
    try:
        import mlflow
        from mlflow.entities import SpanType
    except Exception:  # noqa: BLE001 -- tracing is optional (local dev)
        mlflow = None  # type: ignore[assignment]

    out = Streamed()
    trace_id: str | None = None
    warnings = list(prepared.warnings)
    messages = build_messages(s, prepared.sent_text)

    if mlflow is None:
        async for delta in _pump(d, prepared.sent_text, out):
            yield {"type": "delta", "text": delta}
    else:
        with mlflow.start_span(name="chat", span_type=SpanType.CHAIN) as root:
            trace_id = root.trace_id
            register_intent(trace_id, mode)

            # Record that masking happened without recording any text: this span's
            # input is by definition the original, so it must never hold content.
            if mode != "none":
                with mlflow.start_span(
                    name="guardrails.mask", span_type=SpanType.UNKNOWN
                ) as gspan:
                    gspan.set_inputs({"chars": len(text), "target": "user_message"})
                    gspan.set_outputs({"status": prepared.mask_status, "mode": mode})

            with mlflow.start_span(
                name="chat.completions", span_type=SpanType.CHAT_MODEL
            ) as span:
                async for delta in _pump(d, prepared.sent_text, out):
                    yield {"type": "delta", "text": delta}
                # In `logs` mode these are the ORIGINAL values; the span processor
                # rewrites them when this span ends. In the other modes they are
                # already exactly what we want recorded.
                span.set_inputs({"model": s.llm_model, "messages": messages})
                if out.error:
                    span.set_outputs({"error": "upstream model call failed"})
                else:
                    span.set_outputs({"content": out.text})
                if out.usage is not None:
                    span.set_attribute("mlflow.chat.tokenUsage", out.usage.as_dict())

            # Inputs must be the message VERBATIM, not a dict. MLflow derives a
            # trace's request_preview from this, and the lab's own traces.py documents
            # the convention: "A trace carries request_preview: the user's message,
            # verbatim." Wrapping it in {"message": ..., "mask_mode": ...} made every
            # trace title render as serialised JSON. Mode belongs in an attribute.
            root.set_attribute("presidio_ui.mask_mode", mode)
            root.set_attribute("presidio_ui.mask_status", prepared.mask_status)
            root.set_inputs(prepared.sent_text)
            root.set_outputs(out.text if not out.error else "upstream model call failed")

        # The root span has ended, so nothing else in this trace needs the intent.
        clear_intent(trace_id)

    if out.error:
        yield {"type": "error", "error": "llm_failed", "detail": out.error}
        return

    # Preview of what the trace holds. In `logs` mode the processor is the thing that
    # actually wrote it, so this is computed separately for display -- it hits the same
    # LRU cache the processor just populated, so it costs essentially nothing.
    logged_reply = out.text
    mask_status = prepared.mask_status
    if mode == "logs":
        try:
            logged_reply = await _mask(d, out.text)
        except (MaskError, MaskBlocked):
            logged_reply = SCRUBBED
            mask_status = "degraded"
            warnings.append(
                "Reply masking failed after the answer was produced - "
                "the trace shows a placeholder."
            )

    # The mapping goes to the browser and nowhere else. It is NOT in any span above,
    # and it is withheld in `logs` mode on purpose: there the model gets the original
    # and only the trace is redacted, so a visible handle -> value table would be the
    # key that undoes exactly the redaction that mode exists to demonstrate. In
    # `model` mode it is the opposite -- the reply comes back full of handles and the
    # mapping is the only way to read it.
    yield {
        "type": "meta",
        "mode": mode,
        "mask_status": mask_status,
        "logged_user": prepared.logged_user,
        "logged_reply": logged_reply,
        "trace_id": trace_id,
        "trace_url": trace_url(s, trace_id),
        "usage": out.usage.as_dict() if out.usage else None,
        "warnings": warnings,
        "mapping": [r.as_dict() for r in prepared.mapping] if mode == "model" else [],
        # Tri-state on purpose: null means "not probed yet", which is what the
        # `none` mode reports. The UI must not read that as "cannot map".
        "mapping_supported": d.mapping_supported,
    }
