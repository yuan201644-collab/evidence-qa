"""Server-side model profile catalog and request-scoped selection.

Only public labels and availability are returned to the UI. API keys remain in
the backend environment and are never serialized into a response or trace.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from typing import Iterator


@dataclass(frozen=True)
class ModelProfile:
    id: str
    label: str
    provider: str
    model: str
    base_url: str
    api_key: str
    configured: bool

    def public_dict(self, *, selected: bool = False) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
            "configured": self.configured,
            "selected": selected,
        }


@dataclass(frozen=True)
class ModelProfileDefinition:
    """One OpenAI-compatible model entry used by APIs, settings, and selection."""

    id: str
    label: str
    provider: str
    model_env_key: str
    base_url_env_key: str
    api_key_env_key: str
    default_model: str
    default_base_url: str = ""
    legacy_api_key_env_key: str | None = None
    legacy_api_base_env_key: str | None = None
    legacy_model_markers: tuple[str, ...] = ()


# Add supported models here. The model picker, settings form, and configuration
# writer all consume this registry, so a new compatible model needs one entry.
MODEL_PROFILE_DEFINITIONS: tuple[ModelProfileDefinition, ...] = (
    ModelProfileDefinition(
        id="deepseek-v4-1-flash",
        label="DeepSeek-V4.1-Flash",
        provider="火山方舟",
        model_env_key="DEEPSEEK_MODEL",
        base_url_env_key="DEEPSEEK_API_BASE",
        api_key_env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-v4.1-flash",
        legacy_api_key_env_key="LLM_API_KEY",
        legacy_api_base_env_key="LLM_API_BASE",
        legacy_model_markers=("deepseek",),
    ),
    ModelProfileDefinition(
        id="glm-4-flash",
        label="GLM-4-Flash",
        provider="智谱",
        model_env_key="GLM4_MODEL",
        base_url_env_key="GLM4_API_BASE",
        api_key_env_key="GLM4_API_KEY",
        default_model="glm-4-flash",
        default_base_url="https://open.bigmodel.cn/api/paas/v4/",
        legacy_api_key_env_key="LLM_API_KEY",
        legacy_model_markers=("glm",),
    ),
)


_active_profile: ContextVar[ModelProfile | None] = ContextVar(
    "active_model_profile", default=None
)


def _value(name: str | None, default: str = "") -> str:
    value = os.environ.get(name) if name else None
    return (value or default).strip()


def iter_profile_definitions() -> tuple[ModelProfileDefinition, ...]:
    """Return the single source of truth for supported model profiles."""
    return MODEL_PROFILE_DEFINITIONS


def get_profile_definition(profile_id: str) -> ModelProfileDefinition | None:
    return next(
        (definition for definition in iter_profile_definitions() if definition.id == profile_id),
        None,
    )


def get_profiles() -> tuple[ModelProfile, ...]:
    """Build all registered profiles from backend-only environment values."""
    configured_model = _value("LLM_MODEL").lower()
    profiles: list[ModelProfile] = []
    for definition in iter_profile_definitions():
        legacy_default_selected = any(
            marker in configured_model for marker in definition.legacy_model_markers
        )
        api_key = _value(definition.api_key_env_key)
        if not api_key and legacy_default_selected and definition.legacy_api_key_env_key:
            api_key = _value(definition.legacy_api_key_env_key)

        base_url = (
            _value(definition.base_url_env_key)
            or _value(definition.legacy_api_base_env_key)
            or definition.default_base_url
        )
        model = _value(definition.model_env_key, definition.default_model)
        profiles.append(ModelProfile(
            id=definition.id,
            label=definition.label,
            provider=definition.provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            configured=bool(api_key and base_url and model),
        ))
    return tuple(profiles)


def get_default_profile_id() -> str:
    """Keep the existing .env-selected model as the default request model."""
    model = _value("LLM_MODEL").lower()
    for definition in iter_profile_definitions():
        if any(marker in model for marker in definition.legacy_model_markers):
            return definition.id
    definitions = iter_profile_definitions()
    return definitions[0].id if definitions else "default"


def get_profile(profile_id: str) -> ModelProfile | None:
    return next((profile for profile in get_profiles() if profile.id == profile_id), None)


def get_active_profile() -> ModelProfile | None:
    return _active_profile.get()


@contextmanager
def model_profile_scope(profile_id: str | None) -> Iterator[None]:
    """Select one configured profile for this request and its worker threads."""
    if not profile_id or profile_id == "default":
        yield
        return
    profile = get_profile(profile_id)
    if profile is None:
        raise ValueError("不支持所选模型")
    if not profile.configured:
        raise ValueError("所选模型尚未在后端配置凭据")
    token = _active_profile.set(profile)
    try:
        yield
    finally:
        _active_profile.reset(token)


def public_profile_catalog() -> list[dict[str, object]]:
    default_id = get_default_profile_id()
    return [
        profile.public_dict(selected=profile.id == default_id)
        for profile in get_profiles()
    ]


__all__ = [
    "MODEL_PROFILE_DEFINITIONS",
    "ModelProfile",
    "ModelProfileDefinition",
    "get_active_profile",
    "get_default_profile_id",
    "get_profile",
    "get_profile_definition",
    "get_profiles",
    "iter_profile_definitions",
    "model_profile_scope",
    "public_profile_catalog",
]
