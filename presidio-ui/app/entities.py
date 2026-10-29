"""Entity catalog and the Apply flow behind the checkbox grid.

WHY THIS NEEDS A CONFIGMAP WRITE

Per-request entity selection is impossible in this build. The request schema exposes
guardrails.config ("Inline guardrails configuration to use instead of config_id") but
the /v1/checks handler reads only guardrails.config_ids -- the inline field is accepted,
validated and discarded. So which entities get masked is a property of the loaded
config, and changing it means writing the config.

WHY A DEDICATED CONFIGMAP

The lab's `northgate` config is also consumed by water-plant-ui. Repointing its entity
list from this UI would silently change that app's behaviour. So this app owns its own
config (presidio-ui-guardrails-config) and only ever writes that one.

WHY NO ROLLOUT PERMISSION IS NEEDED

The TrustyAI operator stamps trustyai.opendatahub.io/nemo-config-hash on the guardrails
pod template. Patching the ConfigMap changes that hash and the operator rolls the pod
itself, so this app needs only get/patch on one ConfigMap -- no Deployment RBAC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
import yaml

from .config import Settings
from .tracing import read_sa_token

log = logging.getLogger(__name__)

# Presidio's 19 built-in recognizers in this image, plus our custom one. Verified with
# AnalyzerEngine().get_supported_entities() inside the guardrails pod.
BUILTIN_ENTITIES: list[dict[str, str]] = [
    {"name": "PERSON", "label": "Person name"},
    {"name": "EMAIL_ADDRESS", "label": "Email address"},
    {"name": "PHONE_NUMBER", "label": "Phone number"},
    {"name": "CREDIT_CARD", "label": "Credit card"},
    {"name": "CRYPTO", "label": "Crypto wallet"},
    {"name": "DATE_TIME", "label": "Date / time"},
    {"name": "IBAN_CODE", "label": "IBAN"},
    {"name": "IP_ADDRESS", "label": "IP address"},
    {"name": "LOCATION", "label": "Location"},
    {"name": "MAC_ADDRESS", "label": "MAC address"},
    {"name": "MEDICAL_LICENSE", "label": "Medical license"},
    {"name": "NRP", "label": "Nationality / religion / politics"},
    {"name": "UK_NHS", "label": "UK NHS number"},
    {"name": "URL", "label": "URL"},
    {"name": "US_BANK_NUMBER", "label": "US bank number"},
    {"name": "US_DRIVER_LICENSE", "label": "US driver license"},
    {"name": "US_ITIN", "label": "US ITIN"},
    {"name": "US_PASSPORT", "label": "US passport"},
    {"name": "US_SSN", "label": "US SSN"},
]

CUSTOM_ENTITIES: list[dict[str, str]] = [
    {"name": "SE_PERSONNUMMER", "label": "Swedish personnummer"},
]

# Custom entities sit directly after the three that ship enabled, so the whole
# default selection is visible without scrolling the list.
CATALOG = BUILTIN_ENTITIES[:3] + CUSTOM_ENTITIES + BUILTIN_ENTITIES[3:]
CATALOG_NAMES = {e["name"] for e in CATALOG}

# Keep >= 0.5. mask_sensitive_data ignores the configured score_threshold and pins the
# analyzer at a hardcoded 0.4 in this build, so a lower score would silently never match.
_PERSONNUMMER_RECOGNIZER: dict[str, Any] = {
    "name": "Swedish personnummer",
    "supported_language": "en",
    "supported_entity": "SE_PERSONNUMMER",
    "patterns": [
        {
            "name": "personnummer",
            "regex": r"\b(?:19|20)?[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[-+]?[0-9]{4}\b",
            "score": 0.6,
        }
    ],
    "context": ["personnummer", "pnr", "personal number"],
}


class EntityError(RuntimeError):
    pass


@dataclass(frozen=True)
class EntityState:
    catalog: list[dict[str, str]]
    enabled: list[str]
    editable: bool
    config_id: str


def build_config_yaml(s: Settings, enabled: list[str]) -> str:
    """Render the NeMo config for a given entity selection."""
    entities = [name for name in (e["name"] for e in CATALOG) if name in set(enabled)]
    doc: dict[str, Any] = {
        "colang_version": "1.0",
        # The models block must be carried through every rewrite. Apply regenerates
        # this whole file, so dropping it here would strip the model config out of
        # the guardrails server on the first Apply.
        "models": [
            {
                "type": "main",
                "engine": "openai",
                "model": s.llm_model,
                "api_key_env_var": "OPENAI_API_KEY",
                "parameters": {"base_url": s.llm_base_url, "temperature": 0},
            }
        ],
        "rails": {
            "config": {
                "sensitive_data_detection": {
                    "recognizers": [_PERSONNUMMER_RECOGNIZER],
                    "input": {"entities": entities},
                    "output": {"entities": entities},
                }
            },
            "input": {"flows": ["mask sensitive data on input"]},
            "output": {"flows": ["mask sensitive data on output"]},
        },
    }
    return yaml.dump(doc, default_flow_style=False, sort_keys=False, width=10**6)


def parse_enabled(config_yaml: str) -> list[str]:
    """Read the enabled entity list back out of a rendered config."""
    try:
        doc = yaml.safe_load(config_yaml) or {}
        found = (
            doc.get("rails", {})
            .get("config", {})
            .get("sensitive_data_detection", {})
            .get("input", {})
            .get("entities", [])
        )
    except Exception as exc:  # noqa: BLE001
        raise EntityError(f"could not parse the guardrails config: {exc}") from exc
    return [e for e in found if e in CATALOG_NAMES]


def _k8s_client(s: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=s.k8s_api,
        timeout=20.0,
        verify=s.k8s_ca_path,
        headers={"Authorization": f"Bearer {read_sa_token(s.sa_token_path)}"},
    )


def _cm_path(s: Settings) -> str:
    return f"/api/v1/namespaces/{s.namespace}/configmaps/{s.guardrails_configmap}"


def read_state(s: Settings) -> EntityState:
    """Read the enabled set from the ConfigMap, so the UI reflects reality."""
    if not s.entity_editing_enabled:
        return EntityState(
            catalog=CATALOG, enabled=[], editable=False, config_id=s.nemo_config_id
        )
    try:
        with _k8s_client(s) as client:
            resp = client.get(_cm_path(s))
            resp.raise_for_status()
            data = resp.json().get("data", {})
    except httpx.HTTPError as exc:
        raise EntityError(f"could not read {s.guardrails_configmap}: {exc}") from exc
    return EntityState(
        catalog=CATALOG,
        enabled=parse_enabled(data.get("config.yaml", "")),
        editable=True,
        config_id=s.nemo_config_id,
    )


def apply_state(s: Settings, enabled: list[str]) -> EntityState:
    """Patch the ConfigMap. The operator notices the hash change and rolls the pod."""
    if not s.entity_editing_enabled:
        raise EntityError("entity editing is disabled (NAMESPACE/GUARDRAILS_CONFIGMAP unset)")
    unknown = sorted(set(enabled) - CATALOG_NAMES)
    if unknown:
        raise EntityError(f"unknown entities: {', '.join(unknown)}")

    body = {"data": {"config.yaml": build_config_yaml(s, enabled)}}
    try:
        with _k8s_client(s) as client:
            resp = client.patch(
                _cm_path(s),
                json=body,
                headers={"Content-Type": "application/merge-patch+json"},
            )
            if resp.status_code in (401, 403):
                raise EntityError(
                    "not allowed to patch the guardrails ConfigMap -- the pod's "
                    "ServiceAccount needs get/patch on "
                    f"configmaps/{s.guardrails_configmap}"
                )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise EntityError(f"could not patch {s.guardrails_configmap}: {exc}") from exc

    return read_state(s)


def config_live(s: Settings) -> bool:
    """True once the guardrails server is serving our config id again after a roll."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{s.nemo_url}/v1/rails/configs")
            resp.raise_for_status()
            ids = {row.get("id") for row in resp.json()}
    except Exception:  # noqa: BLE001
        return False
    if not s.nemo_config_id:
        return bool(ids)
    return s.nemo_config_id in ids
