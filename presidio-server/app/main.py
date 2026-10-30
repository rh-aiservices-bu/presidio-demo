"""A Presidio masking service, and nothing more.

Two surfaces:

  /v1/checks   the subset of the NeMo Guardrails API that presidio-ui uses, returning
               the same `<ENTITY_TYPE>` masking the rail produces. Drop-in: point
               NEMO_URL here and nothing else changes.
  /analyze     Presidio's surviving spans for the submitted text. This is the one
               thing NeMo computes and then throws away, and it is what lets a caller
               do its own numbering, pseudonymisation or mapping.

There is deliberately no /mask-with-mapping here, and no handle formatting. Numbering
and the handle -> value mapping are application concerns: they need session scope and
a retention policy, and a shared masking service is the wrong place for either. They
live in presidio-ui.

Endpoints are plain `def`, not `async def`: Presidio's analysis is CPU-bound and
Starlette runs sync endpoints in a worker thread, which is where it belongs.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .config import store_from_env
from .engine import Engine

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("presidio_server")

# Never log message content above DEBUG: this service exists to keep text out of
# places it should not be, and its own log is one of those places.
MAX_CHARS = int(os.environ.get("MAX_CHARS", "20000"))


class Message(BaseModel):
    role: str = "user"
    content: str = ""


class GuardrailsOptions(BaseModel):
    config_ids: list[str] = Field(default_factory=list)


class ChecksRequest(BaseModel):
    """The NeMo /v1/checks request, narrowed to the fields that matter here."""

    model: str | None = None
    messages: list[Message] = Field(default_factory=list)
    guardrails: GuardrailsOptions = Field(default_factory=GuardrailsOptions)


class AnalyzeRequest(BaseModel):
    text: str = Field(max_length=MAX_CHARS)
    entities: list[str] | None = Field(default=None, max_length=64)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.store = store_from_env()
    # Loads the spaCy model, so it must not happen on the first request.
    app.state.engine = Engine(os.environ.get("SPACY_MODEL", "en_core_web_lg"))
    config = app.state.store.current()
    log.info(
        "ready: entities=%s config_id=%s threshold=%s",
        ",".join(config.entities),
        config.config_id,
        config.score_threshold,
    )
    yield


app = FastAPI(title="Presidio masking service", lifespan=lifespan, redoc_url=None)


@app.get("/healthz")
def healthz(request: Request) -> dict[str, Any]:
    config = request.app.state.store.current()
    return {
        "ok": True,
        "config_id": config.config_id,
        "entities": config.entities,
        "score_threshold": config.score_threshold,
        "recognizers": [r.get("supported_entity") for r in config.recognizers],
    }


@app.get("/v1/rails/configs")
def rails_configs(request: Request) -> list[dict[str, str]]:
    """NeMo compatibility: presidio-ui polls this to tell when a config is live."""
    return [{"id": request.app.state.store.current().config_id}]


@app.post("/v1/checks")
def checks(request: Request, body: ChecksRequest) -> dict[str, Any]:
    """NeMo-compatible masking: `<ENTITY_TYPE>` for every match, text only."""
    text = next((m.content for m in reversed(body.messages) if m.role == "user"), None)
    if text is None:
        raise HTTPException(status_code=422, detail="no user message to check")
    if len(text) > MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"text longer than {MAX_CHARS} characters")

    config = request.app.state.store.current()
    engine: Engine = request.app.state.engine
    masked = engine.mask(
        text=text,
        entities=config.entities,
        recognizers=config.recognizers,
        score_threshold=config.score_threshold,
    )
    return {"status": "modified" if masked != text else "passed", "content": masked}


@app.post("/analyze")
def analyze(request: Request, body: AnalyzeRequest) -> dict[str, Any]:
    """Presidio's surviving spans, as offsets into the text you submitted.

    `text` on each span is the caller's own input echoed back, so a caller does not
    have to slice by offset and cannot get the slicing wrong.

    Returning PII locations is a sharper tool than returning masked text: anyone who
    can call this learns exactly where the sensitive data is. The caller already holds
    the text it sent, so this leaks nothing to the caller -- but it is a reason to keep
    the service in-cluster rather than public.
    """
    config = request.app.state.store.current()
    engine: Engine = request.app.state.engine
    spans = engine.spans(
        text=body.text,
        entities=body.entities if body.entities else config.entities,
        recognizers=config.recognizers,
        score_threshold=config.score_threshold,
    )
    return {
        "spans": [s.as_dict() for s in spans],
        "entities": body.entities if body.entities else config.entities,
    }
