"""MLflow tracing: export wiring, plus the span processor that implements `logs` mode.

WHY A SPAN PROCESSOR AND NOT RECORD-TIME MASKING

`logs` mode has to redact the trace while the model still receives the original. The
naive approach -- mask the values we hand to set_inputs()/set_outputs() -- only covers
spans this app authors. As soon as there is agentic work (tool calls, retrievers,
sub-agent steps) most spans come from instrumentation we do not create, and PII in tool
arguments sails straight through.

mlflow.tracing.configure(span_processors=[...]) registers a callback MLflow invokes once
per span, synchronously inside LiveSpan.end(), before the span is queued for export. It
therefore sees *every* span regardless of origin, which is what makes `logs` mode work
unchanged when tools are added.

THREE TRAPS, ALL LOAD-BEARING

1. mlflow.tracing.utils.processor.apply_span_processors wraps each processor call in
   try/except and SWALLOWS the exception -- the span would then export UNMASKED. That
   only applies to exceptions escaping our function, so we catch everything ourselves
   and scrub. We never raise, so the swallow never applies. A registry miss is likewise
   treated as `logs` (scrub), never as pass-through.

2. mlflow.tracing.configure() mutates an unguarded module global. Its context-manager
   form must NEVER be used per request -- one request's exit clobbers another's config.
   Register once, at startup, function form only.

   On cost: every string in a `logs`-mode span now goes to NeMo unless it provably
   cannot contain an entity (see masking.worth_checking). That is deliberate -- the
   earlier "guess what Presidio will flag" pre-filter leaked. The LRU cache below plus
   skipping non-`logs` traces is what keeps it affordable.

3. mlflow.tracing.reset() calls reset_config(), which wipes span_processors. Never call
   it -- it would silently disable masking.

Also: validate_span_processors rejects anything whose __code__.co_argcount != 1, so the
processor must be a module-level function, not a bound method.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import httpx
from cachetools import LRUCache, TTLCache

from .config import Settings
from .masking import MaskError, mask_text_sync, worth_checking

log = logging.getLogger(__name__)

PLACEHOLDER = "<redacted: logs mode>"
SCRUBBED = "<redacted: masking unavailable>"

# --- module state, populated by install_tracing ---------------------------------
_SETTINGS: Settings | None = None
_SYNC_HTTP: httpx.Client | None = None
_EXPERIMENT_ID: str | None = None

# trace_id -> mode. TTL and maxsize bound a missed cleanup, so a leaked entry
# self-heals rather than growing without limit.
_INTENTS: TTLCache[str, str] = TTLCache(maxsize=4096, ttl=600)
_INTENT_LOCK = threading.Lock()

# Masked-text cache. Agentic spans echo the same strings constantly (tool args repeat
# the user's request), so the hit rate is high and this is what keeps the synchronous
# processor affordable.
_MASK_CACHE: LRUCache[str, str] = LRUCache(maxsize=2048)
_MASK_LOCK = threading.Lock()


@dataclass
class TracingHealth:
    enabled: bool = False
    experiment_id: str | None = None
    warmup_flushed: bool = False
    span_type: str | None = None
    spans_seen: int = 0
    spans_masked: int = 0
    spans_scrubbed: int = 0
    last_error: str | None = None
    last_error_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


HEALTH = TracingHealth()


# --- auth ------------------------------------------------------------------------
_token_cache: tuple[float, str] | None = None


def read_sa_token(path: str) -> str:
    """Read the projected ServiceAccount token, re-reading when the file changes.

    The projected volume requests 3607s, but this cluster issues the token with a
    365-day expiry, so a value captured at startup stays valid for the life of any
    workshop. The mtime check costs nothing and keeps us correct if that changes.
    """
    global _token_cache
    try:
        mtime = os.path.getmtime(path)
    except OSError as exc:
        raise RuntimeError(f"cannot read service account token at {path}") from exc
    if _token_cache and _token_cache[0] == mtime:
        return _token_cache[1]
    with open(path, encoding="utf-8") as fh:
        token = fh.read().strip()
    _token_cache = (mtime, token)
    return token


# --- experiment resolution -------------------------------------------------------
def resolve_experiment_id(s: Settings) -> str:
    """Resolve the experiment by name, creating it if absent. Returns its id.

    Note the DOUBLED /mlflow in the REST path: <base>/mlflow/api/2.0/mlflow/...
    The bare /api/2.0/mlflow/... form returns 404. And never use the browser URL --
    it sits behind an OAuth proxy that answers writes with a login page as HTTP 200.
    """
    base = f"{s.mlflow_rest_base}/mlflow/api/2.0/mlflow"
    headers = {
        "Authorization": f"Bearer {read_sa_token(s.sa_token_path)}",
        "X-MLflow-Workspace": s.mlflow_workspace,
        "Content-Type": "application/json",
    }
    verify = s.mlflow_ca_file if os.path.exists(s.mlflow_ca_file) else True

    with httpx.Client(timeout=30.0, verify=verify, headers=headers) as client:
        resp = client.get(
            f"{base}/experiments/get-by-name",
            params={"experiment_name": s.mlflow_experiment_name},
        )
        if resp.status_code == 200:
            return str(resp.json()["experiment"]["experiment_id"])
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"MLflow refused the experiment lookup ({resp.status_code}). The pod's "
                "ServiceAccount needs get/list/create/update on "
                "experiments.mlflow.kubeflow.org in this namespace. Note the authorizer "
                "caches denials for 300s, so a fresh RoleBinding takes up to 5 minutes."
            )
        if resp.status_code != 404:
            resp.raise_for_status()

        created = client.post(
            f"{base}/experiments/create", json={"name": s.mlflow_experiment_name}
        )
        if created.status_code == 200:
            return str(created.json()["experiment_id"])
        # Lost a create race with another replica: re-resolve.
        again = client.get(
            f"{base}/experiments/get-by-name",
            params={"experiment_name": s.mlflow_experiment_name},
        )
        again.raise_for_status()
        return str(again.json()["experiment"]["experiment_id"])


# --- the mode registry -----------------------------------------------------------
def register_intent(trace_id: str, mode: str) -> None:
    if not trace_id:
        return
    with _INTENT_LOCK:
        _INTENTS[trace_id] = mode


def clear_intent(trace_id: str) -> None:
    if not trace_id:
        return
    with _INTENT_LOCK:
        _INTENTS.pop(trace_id, None)


def _lookup_intent(trace_id: str) -> str:
    # Miss -> "logs", i.e. scrub. If the registry ever breaks wholesale every trace
    # becomes placeholders: the demo looks broken but nothing leaks. That is the
    # correct direction of failure.
    with _INTENT_LOCK:
        return _INTENTS.get(trace_id, "logs")


# --- masking helpers used by the processor ---------------------------------------
def _mask_cached(text: str) -> str:
    with _MASK_LOCK:
        hit = _MASK_CACHE.get(text)
    if hit is not None:
        return hit
    assert _SETTINGS is not None and _SYNC_HTTP is not None
    result = mask_text_sync(
        _SYNC_HTTP,
        _SETTINGS.nemo_url,
        _SETTINGS.llm_model,
        _SETTINGS.nemo_config_id,
        text,
    )
    with _MASK_LOCK:
        _MASK_CACHE[text] = result.content
    return result.content


def _mask_obj(value: Any, depth: int = 0) -> Any:
    """Recursively mask every string inside a span's inputs/outputs."""
    if depth > 6:
        return PLACEHOLDER
    if isinstance(value, str):
        return _mask_cached(value) if worth_checking(value) else value
    if isinstance(value, dict):
        return {k: _mask_obj(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_obj(v, depth + 1) for v in value]
    return value


def _scrub(span: Any) -> None:
    """Last resort: replace content with a fixed string. Must not be able to fail."""
    try:
        span.set_inputs({"redacted": SCRUBBED})
    except Exception:  # noqa: BLE001
        pass
    try:
        span.set_outputs({"redacted": SCRUBBED})
    except Exception:  # noqa: BLE001
        pass
    try:
        span.set_attribute("presidio_ui.redaction", "scrubbed")
    except Exception:  # noqa: BLE001
        pass
    HEALTH.spans_scrubbed += 1


def presidio_ui_trace_guard(span) -> None:  # noqa: ANN001 -- MLflow requires exactly 1 arg
    """Mask spans belonging to a `logs`-mode trace. Module-level, single argument."""
    try:
        HEALTH.spans_seen += 1
        mode = _lookup_intent(getattr(span, "trace_id", "") or "")
        if mode in ("none", "model"):
            return  # nothing unmasked to hide: the recorded text is already correct

        inputs = span.inputs
        outputs = span.outputs
        blob = ""
        try:
            blob = json.dumps([inputs, outputs], default=str)
        except Exception:  # noqa: BLE001
            blob = f"{inputs}{outputs}"
        if not worth_checking(blob):
            return  # provably nothing maskable here (no alphanumeric content at all)

        if inputs is not None:
            span.set_inputs(_mask_obj(inputs))
        if outputs is not None:
            span.set_outputs(_mask_obj(outputs))
        span.set_attribute("presidio_ui.redaction", "processor")
        HEALTH.spans_masked += 1
    except MaskError as exc:
        HEALTH.last_error = f"mask failed in processor: {exc}"
        HEALTH.last_error_at = time.time()
        _scrub(span)
    except Exception as exc:  # noqa: BLE001 -- must never escape; see module docstring
        HEALTH.last_error = f"processor error: {type(exc).__name__}"
        HEALTH.last_error_at = time.time()
        _scrub(span)


# --- install ---------------------------------------------------------------------
def install_tracing(s: Settings) -> str | None:
    """Wire up MLflow tracing. Returns the experiment id, or None if disabled.

    Order matters: the OTel env vars must be set before the first span, because MLflow
    builds its exporter lazily and reads the environment exactly once.
    """
    global _SETTINGS, _SYNC_HTTP, _EXPERIMENT_ID

    _SETTINGS = s
    _SYNC_HTTP = httpx.Client(timeout=s.nemo_timeout_s)

    if not s.tracing_enabled:
        log.warning("tracing disabled (no OTLP endpoint / MLflow base configured)")
        HEALTH.enabled = False
        return None

    # MLflow fetches a telemetry config URL at import unless these are set.
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    os.environ.setdefault("DO_NOT_TRACK", "true")

    # MANDATORY, and the reason is not obvious. Even on the pure OTLP path,
    # mlflow.start_span() calls _resolve_experiment_uc_location() -> get_tracking_uri()
    # -> _get_default_tracking_uri() -> _has_existing_mlruns_data(), which imports the
    # full-mlflow FileStore and therefore sqlparse. The slim mlflow-tracing package does
    # not ship sqlparse, the import raises, start_span's broad `except Exception`
    # swallows it, and every span silently becomes a NoOpSpan -- no traces at all, with
    # nothing logged above DEBUG. Setting this short-circuits the whole chain.
    os.environ.setdefault("MLFLOW_TRACKING_URI", s.mlflow_rest_base)

    exp_id = resolve_experiment_id(s)
    _EXPERIMENT_ID = exp_id

    # MLflow defaults the OTLP protocol to grpc; our endpoint is HTTPS /v1/traces, so
    # without this nothing exports at all.
    os.environ["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] = "http/protobuf"
    # Per-client CA. Deliberately NOT SSL_CERT_FILE/REQUESTS_CA_BUNDLE -- pointing those
    # at MLflow's service CA alone breaks outbound HTTPS to the MaaS model endpoint.
    if os.path.exists(s.mlflow_ca_file):
        os.environ["OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE"] = s.mlflow_ca_file
    os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = ",".join(
        [
            f"x-mlflow-experiment-id={exp_id}",
            f"X-MLflow-Workspace={s.mlflow_workspace}",
            f"Authorization=Bearer {read_sa_token(s.sa_token_path)}",
        ]
    )

    import mlflow  # imported late: the env above must be in place first

    if s.trace_backstop:
        mlflow.tracing.configure(span_processors=[presidio_ui_trace_guard])
        log.info("registered presidio_ui_trace_guard span processor")
    else:
        log.warning("TRACE_BACKSTOP is off: `logs` mode will NOT redact traces")

    HEALTH.enabled = True
    HEALTH.experiment_id = exp_id

    # Warm-up: one PII-free trace, force-flushed, so a broken export path fails at
    # startup rather than being discovered later from an empty MLflow UI.
    try:
        with mlflow.start_span(name="startup") as span:
            # Assert we got a real span. start_span() degrades to NoOpSpan on ANY
            # internal error and logs nothing above DEBUG, so without this check a
            # completely untraced deployment looks healthy. See the MLFLOW_TRACKING_URI
            # note above for the failure this actually caught.
            span_kind = type(span).__name__
            HEALTH.span_type = span_kind
            if span_kind == "NoOpSpan":
                raise RuntimeError(
                    "MLflow returned a NoOpSpan: tracing is silently disabled. Usually a "
                    "missing dependency inside mlflow.start_span being swallowed -- run "
                    "with LOG_LEVEL=DEBUG to see the real exception."
                )
            register_intent(span.trace_id, "none")
            span.set_inputs({"warmup": True})
            span.set_outputs({"ok": True})
            tid = span.trace_id
        clear_intent(tid)
        HEALTH.warmup_flushed = bool(flush())
    except Exception as exc:  # noqa: BLE001
        HEALTH.last_error = f"warmup failed: {type(exc).__name__}: {exc}"
        HEALTH.last_error_at = time.time()
        log.error("tracing warm-up failed: %s", exc)

    return exp_id


def flush(timeout_millis: int = 10_000) -> bool:
    """Force-flush pending spans. Returns True only if a flush actually happened.

    MLflow 3.14 exposes no public force_flush for the OTLP path: the SDK provider is
    hidden behind mlflow.tracing.provider.provider._isolated_tracer_provider. We walk
    to it defensively rather than importing a private name, so a rename degrades to
    "no explicit flush" instead of an exception. Spans still leave on the
    BatchSpanProcessor's own schedule (~5s) either way -- this only matters for the
    startup warm-up check and for not dropping spans at shutdown.
    """
    flushed = False
    try:
        import mlflow

        public = getattr(mlflow, "flush_trace_async_logging", None)
        if callable(public):
            public()
    except Exception as exc:  # noqa: BLE001
        log.debug("flush_trace_async_logging unavailable: %s", type(exc).__name__)

    try:
        from mlflow.tracing import provider as _prov

        candidates = [getattr(_prov, "provider", None)]
        wrapper = candidates[0]
        if wrapper is not None:
            candidates += [getattr(wrapper, name) for name in vars(wrapper)]
        for candidate in candidates:
            force = getattr(candidate, "force_flush", None)
            if callable(force):
                flushed = bool(force(timeout_millis=timeout_millis))
                break
        else:
            log.warning(
                "no force_flush found on the MLflow tracer provider; relying on the "
                "batch processor's own export interval"
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("force_flush failed: %s: %s", type(exc).__name__, exc)
    return flushed


def shutdown() -> None:
    global _SYNC_HTTP
    try:
        flush()
    finally:
        if _SYNC_HTTP is not None:
            _SYNC_HTTP.close()
            _SYNC_HTTP = None


def experiment_id() -> str | None:
    return _EXPERIMENT_ID


def tracing_health() -> dict[str, Any]:
    return HEALTH.as_dict()
