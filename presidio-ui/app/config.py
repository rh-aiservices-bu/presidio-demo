"""Settings, read from the environment (populated by the presidio-ui-config ConfigMap).

Fails fast on anything the app cannot work without, so a misconfigured Deployment
dies at startup with a clear message rather than 500-ing on the first message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ConfigError(f"{name} is required but unset or empty")
    return val


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- LLM (OpenAI-compatible) ---
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_timeout_s: float = 300.0
    system_prompt: str = ""

    # --- NeMo Guardrails masking service ---
    nemo_url: str = "http://nemo-guardrails:80"
    # How detected entities are named. The masking service only detects; naming is
    # this app's job (see handles.py), so these live here and not in its config.
    mask_style: str = "indexed"
    mask_token: str = ""
    # "word" names each word of a PERSON span, so a later bare "Sara" reuses the handle
    # issued for "Sara Banderby". See the granularity note in handles.py.
    mask_granularity: str = "word"
    mask_word_entities: tuple[str, ...] = ("PERSON",)
    # Empty means "let the server pick its default config". Set to the dedicated
    # config id the chart registers (values: guardrails.configId).
    nemo_config_id: str = ""
    nemo_timeout_s: float = 20.0

    # --- MLflow ---
    # Absent OTLP endpoint disables tracing entirely (local development).
    otlp_endpoint: str = ""
    mlflow_rest_base: str = ""
    mlflow_workspace: str = ""
    mlflow_experiment_name: str = ""
    mlflow_browser_url: str = ""
    mlflow_ca_file: str = "/etc/mlflow-ca/service-ca.crt"
    trace_backstop: bool = True

    # --- Kubernetes (for the entity Apply button) ---
    namespace: str = ""
    guardrails_configmap: str = ""
    sa_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    k8s_ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    k8s_api: str = "https://kubernetes.default.svc"

    # --- misc ---
    max_concurrent_llm: int = 8
    leak_test_enabled: bool = False

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.otlp_endpoint and self.mlflow_rest_base and self.mlflow_workspace)

    @property
    def entity_editing_enabled(self) -> bool:
        return bool(self.namespace and self.guardrails_configmap)


def _mask_style() -> str:
    from .handles import STYLES

    style = _opt("MASK_STYLE", "indexed")
    if style not in STYLES:
        raise ConfigError(f"MASK_STYLE={style!r} is not one of {', '.join(STYLES)}")
    return style


def _mask_token() -> str:
    """Optional template overriding the style's format. Validated at startup, so a
    typo is a failed boot rather than a surprise on the first masked message."""
    from .handles import validate_template

    token = _opt("MASK_TOKEN")
    if not token:
        return ""
    try:
        return validate_template(token)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _mask_granularity() -> str:
    from .handles import GRANULARITIES

    value = _opt("MASK_GRANULARITY", "word")
    if value not in GRANULARITIES:
        raise ConfigError(
            f"MASK_GRANULARITY={value!r} is not one of {', '.join(GRANULARITIES)}"
        )
    return value


def _mask_word_entities() -> tuple[str, ...]:
    """Entity types split into words. Only types where a word means something."""
    from .handles import DEFAULT_WORD_ENTITIES

    raw = _opt("MASK_WORD_ENTITIES")
    if not raw:
        return DEFAULT_WORD_ENTITIES
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_settings() -> Settings:
    return Settings(
        llm_base_url=_req("LLM_BASE_URL").rstrip("/"),
        llm_model=_req("LLM_MODEL"),
        llm_api_key=_req("MODEL_API_KEY"),
        llm_timeout_s=float(_opt("LLM_TIMEOUT_S", "300")),
        system_prompt=_opt("SYSTEM_PROMPT"),
        nemo_url=_opt("NEMO_URL", "http://nemo-guardrails:80").rstrip("/"),
        mask_style=_mask_style(),
        mask_token=_mask_token(),
        mask_granularity=_mask_granularity(),
        mask_word_entities=_mask_word_entities(),
        nemo_config_id=_opt("NEMO_CONFIG_ID"),
        nemo_timeout_s=float(_opt("NEMO_TIMEOUT_S", "20")),
        otlp_endpoint=_opt("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"),
        mlflow_rest_base=_opt("MLFLOW_REST_BASE").rstrip("/"),
        mlflow_workspace=_opt("MLFLOW_WORKSPACE"),
        mlflow_experiment_name=_opt("MLFLOW_EXPERIMENT_NAME"),
        mlflow_browser_url=_opt("MLFLOW_URL").rstrip("/"),
        mlflow_ca_file=_opt("MLFLOW_CA_FILE", "/etc/mlflow-ca/service-ca.crt"),
        trace_backstop=_flag("TRACE_BACKSTOP", True),
        namespace=_opt("NAMESPACE"),
        guardrails_configmap=_opt("GUARDRAILS_CONFIGMAP"),
        max_concurrent_llm=int(_opt("MAX_CONCURRENT_LLM", "8")),
        leak_test_enabled=_flag("LEAK_TEST_ENABLED", False),
    )
