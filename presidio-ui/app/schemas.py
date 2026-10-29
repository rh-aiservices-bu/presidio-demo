"""Request/response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Mode = Literal["none", "model", "logs"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    mode: Mode = "none"
    session_id: str | None = Field(default=None, max_length=128)


class EntityApplyRequest(BaseModel):
    enabled: list[str] = Field(default_factory=list, max_length=64)


class EntityStateResponse(BaseModel):
    catalog: list[dict[str, str]]
    enabled: list[str]
    editable: bool
    config_id: str
    live: bool


class ModeInfo(BaseModel):
    id: Mode
    label: str
    detail: str


MODES: list[ModeInfo] = [
    ModeInfo(
        id="none",
        label="No masking",
        detail="The model and MLflow both see your original text.",
    ),
    ModeInfo(
        id="model",
        label="Mask for the model",
        detail="The model receives masked text, so the trace records masked text too.",
    ),
    ModeInfo(
        id="logs",
        label="Mask for the logs",
        detail="The model receives your original text; MLflow records only placeholders.",
    ),
]
