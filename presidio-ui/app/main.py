"""FastAPI app: chat with three masking modes, plus the entity Apply flow."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import entities as ent
from . import pipeline, tracing
from .config import ConfigError, load_settings
from .llm import build_client
from .masking import MaskBlocked, MaskError
from .schemas import MODES, ChatRequest, EntityApplyRequest

# Never log message content above DEBUG: an INFO-level dump of a prompt would defeat
# the whole point of `logs` mode.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("presidio_ui")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    app.state.settings = settings
    app.state.http = httpx.AsyncClient(timeout=settings.nemo_timeout_s)
    app.state.oai = build_client(settings)
    app.state.llm_sem = asyncio.Semaphore(settings.max_concurrent_llm)

    # Must happen before the first span: MLflow builds its exporter lazily and reads
    # the OTel environment exactly once.
    try:
        tracing.install_tracing(settings)
    except Exception as exc:  # noqa: BLE001 -- a tracing outage must not stop the chat
        log.error("tracing setup failed, continuing untraced: %s", exc)
        tracing.HEALTH.last_error = f"install failed: {exc}"

    app.state.deps = pipeline.Deps(
        settings=settings, http=app.state.http, oai=app.state.oai
    )
    log.info(
        "ready: model=%s nemo_config=%s tracing=%s",
        settings.llm_model,
        settings.nemo_config_id or "(server default)",
        tracing.HEALTH.enabled,
    )
    try:
        yield
    finally:
        tracing.shutdown()
        await app.state.http.aclose()


app = FastAPI(title="Presidio UI", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Liveness and readiness.

    Deliberately stays healthy when MLflow or NeMo are down -- an unrelated outage
    must not CrashLoop the pod. The detail is reported for diagnosis instead.
    """
    s = request.app.state.settings
    return JSONResponse(
        {
            "ok": True,
            "llm": {"base_url": s.llm_base_url, "model": s.llm_model},
            "nemo": {"url": s.nemo_url, "config_id": s.nemo_config_id or None},
            "mlflow": tracing.tracing_health(),
        }
    )


@app.get("/api/config")
async def api_config(request: Request) -> dict[str, Any]:
    s = request.app.state.settings
    return {
        "model": s.llm_model,
        "modes": [m.model_dump() for m in MODES],
        "mlflow_url": pipeline.trace_url(s),
        "tracing_enabled": tracing.HEALTH.enabled,
        "entity_editing": s.entity_editing_enabled,
    }


@app.get("/api/entities")
async def api_entities_get(request: Request) -> dict[str, Any]:
    s = request.app.state.settings
    try:
        state = await asyncio.to_thread(ent.read_state, s)
    except ent.EntityError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "catalog": state.catalog,
        "enabled": state.enabled,
        "editable": state.editable,
        "config_id": state.config_id,
        "live": await asyncio.to_thread(ent.config_live, s),
    }


@app.post("/api/entities")
async def api_entities_apply(
    request: Request, body: EntityApplyRequest
) -> dict[str, Any]:
    """Patch the guardrails ConfigMap. The operator rolls the pod from the hash change,
    so the new selection becomes live in roughly 30 seconds."""
    s = request.app.state.settings
    try:
        state = await asyncio.to_thread(ent.apply_state, s, body.enabled)
    except ent.EntityError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "catalog": state.catalog,
        "enabled": state.enabled,
        "editable": state.editable,
        "config_id": state.config_id,
        "live": False,
        "note": "The guardrails pod is restarting; this takes about 30 seconds.",
    }


@app.get("/api/entities/live")
async def api_entities_live(request: Request) -> dict[str, bool]:
    s = request.app.state.settings
    return {"live": await asyncio.to_thread(ent.config_live, s)}


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


@app.post("/api/chat")
async def api_chat(request: Request, body: ChatRequest) -> Any:
    deps: pipeline.Deps = request.app.state.deps
    sem: asyncio.Semaphore = request.app.state.llm_sem

    # Pre-LLM masking happens before the stream opens, so `model` mode can still fail
    # closed with a real status code rather than an error buried in a 200 stream.
    try:
        prepared = await pipeline.prepare(deps, body.message, body.mode)
    except MaskBlocked as exc:
        return JSONResponse(
            status_code=200,
            content={
                "type": "blocked",
                "mask_status": "blocked",
                "detail": str(exc),
            },
        )
    except MaskError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": "mask_unavailable", "detail": str(exc)},
        )

    async def gen() -> AsyncIterator[str]:
        async with sem:
            async for event in pipeline.stream_turn(
                deps, body.message, body.mode, prepared
            ):
                yield _sse(event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/debug/leak-test")
async def leak_test(request: Request) -> dict[str, Any]:
    """Prove the span processor covers spans this app does not author.

    Creates a span with PII that is NOT marked as already-masked, inside a trace
    registered as `logs` mode -- standing in for a tool-call span. If the processor
    is working, the exported span holds placeholders. This is the regression test to
    re-run when agentic tools are added.
    """
    s = request.app.state.settings
    if not s.leak_test_enabled:
        raise HTTPException(status_code=404, detail="not enabled")
    try:
        import mlflow
        from mlflow.entities import SpanType
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="tracing unavailable") from exc

    payload = "Dana Whitfield, dana.w@example.com, 212-555-0199, pnr 900116-5678"
    with mlflow.start_span(name="leak-test", span_type=SpanType.CHAIN) as root:
        trace_id = root.trace_id
        tracing.register_intent(trace_id, "logs")
        with mlflow.start_span(name="pretend.tool.call", span_type=SpanType.TOOL) as tool:
            tool.set_inputs({"query": payload})
            tool.set_outputs({"rows": [{"note": payload}]})
        root.set_inputs({"message": payload, "mask_mode": "logs"})
        root.set_outputs({"reply": payload})
    tracing.clear_intent(trace_id)
    tracing.flush()
    return {
        "trace_id": trace_id,
        "trace_url": pipeline.trace_url(s, trace_id),
        "expectation": "every span above should show placeholders, not the PII",
    }


@app.exception_handler(ConfigError)
async def config_error_handler(_: Request, exc: ConfigError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "misconfigured", "detail": str(exc)})
