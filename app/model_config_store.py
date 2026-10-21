"""Local persistence and validation for editable LLM profile configuration.

Credentials are stored in the ignored project .env file and kept in the backend
process environment. Public configuration views expose only whether a key exists.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit

from app.model_profiles import (
    ModelProfileDefinition,
    get_profile,
    get_profile_definition,
    iter_profile_definitions,
)


MODEL_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
_WRITE_LOCK = threading.Lock()
_MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$")


class ModelConfigError(ValueError):
    """A safe, user-facing model configuration validation or save error."""


def _profile_definition(profile_id: str) -> ModelProfileDefinition:
    definition = get_profile_definition(profile_id)
    if definition is None:
        raise ModelConfigError("不支持配置该模型")
    return definition


def _validated_model_id(model: str) -> str:
    value = model.strip()
    if not _MODEL_ID_PATTERN.fullmatch(value):
        raise ModelConfigError("模型 ID 格式不正确")
    return value


def _validated_base_url(base_url: str) -> str:
    value = base_url.strip()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        # Reading .port also validates malformed port syntax.
        _ = parsed.port
    except ValueError as exc:
        raise ModelConfigError("API 地址格式不正确") from exc
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelConfigError("API 地址不能包含账号、密码、查询参数或片段")
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http":
        is_loopback = host.lower() == "localhost" or host.lower().endswith(".localhost")
        try:
            is_loopback = is_loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not is_loopback:
            raise ModelConfigError("公网 API 地址必须使用 HTTPS；HTTP 仅允许本机回环地址")
    else:
        raise ModelConfigError("API 地址必须使用 HTTPS")
    if any(character.isspace() for character in value) or "\r" in value or "\n" in value:
        raise ModelConfigError("API 地址格式不正确")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _validated_api_key(api_key: str | None) -> str:
    value = (api_key or "").strip()
    if len(value) > 4096 or "\r" in value or "\n" in value:
        raise ModelConfigError("API 密钥格式不正确")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        raise ModelConfigError("API 密钥不能以引号包裹")
    return value


def _write_env_values(path: Path, values: dict[str, str]) -> None:
    """Atomically update selected keys while preserving unrelated .env lines."""
    try:
        original_lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        original_lines = []

    pattern = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    output: list[str] = []
    written: set[str] = set()
    for line in original_lines:
        match = pattern.match(line.strip())
        key = match.group(1) if match else None
        if key not in values:
            output.append(line)
        elif key not in written:
            # Values accepted by validators contain no line breaks. The project's
            # dotenv reader treats everything after the first '=' as the value.
            output.append(f"{key}={values[key]}")
            written.add(key)
        # Drop duplicate definitions so the saved value is unambiguous on restart.

    for key, value in values.items():
        if key not in written:
            output.append(f"{key}={value}")
    content = "\n".join(output) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f"{path.name}.", suffix=".tmp", delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ModelConfigError("无法保存模型配置，请检查服务端配置文件权限") from exc


def editable_profile_catalog() -> list[dict[str, object]]:
    """Return editable model fields without ever returning API key values."""
    result: list[dict[str, object]] = []
    for definition in iter_profile_definitions():
        profile = get_profile(definition.id)
        if profile is None:
            continue
        result.append({
            "id": profile.id,
            "label": profile.label,
            "provider": profile.provider,
            "model": profile.model,
            "base_url": profile.base_url,
            "configured": profile.configured,
            "has_api_key": bool(profile.api_key),
        })
    return result


def save_profile_config(
    profile_id: str,
    *,
    model: str,
    base_url: str,
    api_key: str | None,
    env_file: Path | None = None,
) -> dict[str, object]:
    """Persist a profile and activate it without returning the submitted secret."""
    definition = _profile_definition(profile_id)
    normalized_model = _validated_model_id(model)
    normalized_base_url = _validated_base_url(base_url)
    normalized_api_key = _validated_api_key(api_key)
    values = {
        definition.model_env_key: normalized_model,
        definition.base_url_env_key: normalized_base_url,
    }
    if normalized_api_key:
        values[definition.api_key_env_key] = normalized_api_key

    with _WRITE_LOCK:
        _write_env_values(env_file or MODEL_ENV_FILE, values)
        for name, value in values.items():
            os.environ[name] = value

    return next(item for item in editable_profile_catalog() if item["id"] == profile_id)

