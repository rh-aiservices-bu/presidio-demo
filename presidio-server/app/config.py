"""Configuration, in the NeMo Guardrails config.yaml shape.

Reading NeMo's own schema is deliberate: the same ConfigMap can be mounted into this
service and into a guardrails server, so the UI's entity checkboxes keep writing one
file and nothing in presidio-ui has to know which engine is behind the endpoint.

    rails:
      config:
        sensitive_data_detection:
          recognizers: [...]
          input:
            entities: [PERSON, ...]
            score_threshold: 0.4

Unlike the guardrails server, a changed file is picked up without a restart: mounted
ConfigMaps update in place, so the mtime is checked per request. That removes the
30-second pod roll the chart's Apply button otherwise needs.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml



log = logging.getLogger(__name__)

DEFAULT_ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"]


@dataclass(frozen=True)
class MaskConfig:
    entities: list[str] = field(default_factory=lambda: list(DEFAULT_ENTITIES))
    recognizers: list[dict] = field(default_factory=list)
    score_threshold: float = 0.4
    config_id: str = "presidio"


def parse(doc: dict, config_id: str) -> MaskConfig:
    """Read a NeMo-shaped config document. Unknown keys are ignored."""
    sdd = (doc.get("rails") or {}).get("config", {}).get("sensitive_data_detection", {}) or {}
    source = sdd.get("input") or {}

    entities = source.get("entities")
    if not isinstance(entities, list) or not entities:
        entities = list(DEFAULT_ENTITIES)

    recognizers = sdd.get("recognizers")
    if not isinstance(recognizers, list):
        recognizers = []

    try:
        score_threshold = float(source.get("score_threshold", 0.4))
    except (TypeError, ValueError):
        score_threshold = 0.4

    return MaskConfig(
        entities=[str(e) for e in entities],
        recognizers=[r for r in recognizers if isinstance(r, dict)],
        score_threshold=score_threshold,
        config_id=config_id,
    )


class ConfigStore:
    """The current config, reloaded when the file underneath it changes."""

    def __init__(self, path: str | None, config_id: str) -> None:
        self._path = Path(path) if path else None
        self._config_id = config_id
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._config = MaskConfig(config_id=config_id)
        if self._path:
            self.current()

    def current(self) -> MaskConfig:
        if self._path is None:
            return self._config
        try:
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            log.warning("could not stat %s, keeping the loaded config: %s", self._path, exc)
            return self._config
        with self._lock:
            if mtime == self._mtime:
                return self._config
            try:
                doc = yaml.safe_load(self._path.read_text()) or {}
                self._config = parse(doc, self._config_id)
                self._mtime = mtime
                log.info(
                    "loaded config: entities=%s threshold=%s",
                    ",".join(self._config.entities),
                    self._config.score_threshold,
                )
            except Exception as exc:  # noqa: BLE001 -- a bad edit must not take the service down
                log.error("could not load %s, keeping the previous config: %s", self._path, exc)
            return self._config


def store_from_env() -> ConfigStore:
    return ConfigStore(
        path=os.environ.get("CONFIG_PATH") or None,
        config_id=os.environ.get("CONFIG_ID", "presidio"),
    )
