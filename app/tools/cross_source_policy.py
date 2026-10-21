"""跨源问答安全策略：早停原因和可见映射依据规则。"""

from __future__ import annotations

import json
from functools import lru_cache
import os
from pathlib import Path
from typing import Literal


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MAPPING_POLICY_PATH = _PROJECT_ROOT / "data" / "cross_source_demo" / "mapping_basis_policy.json"
MappingBasisStatus = Literal["allowed", "denied", "undefined"]

EARLY_STOP_ALLOWED_REASONS: frozenset[str] = frozenset({
    "mapping_unresolved",
    "equipment_master_missing",
    "mapping_basis_unapproved",
    "validity_window_not_cover",
    "contract_status_inactive",
    "key_event_missing",
    "customer_scope_conflict",
    "event_time_reversed",
    "ambiguous_active_contracts",
})

_ACTIVE_CONTRACT_STATUS_VALUES: frozenset[str] = frozenset({"active", "生效"})


@lru_cache(maxsize=8)
def _load_mapping_basis_policy(resolved_path: str) -> dict[str, object]:
    """读取带版本/来源的映射依据规则；错误配置必须显式失败。"""
    policy_path = Path(resolved_path)
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("mapping basis policy must be an object")
    for key in ("policy_id", "version", "source", "allowed", "denied"):
        if key not in payload:
            raise ValueError(f"mapping basis policy missing {key}")

    def basis_codes(value: object, field: str, *, required: bool) -> list[str]:
        if not isinstance(value, list) or (required and not value):
            qualifier = "non-empty " if required else ""
            raise ValueError(f"mapping basis policy {field} must be a {qualifier}list")
        codes: list[str] = []
        for item in value:
            if isinstance(item, str):
                code = item
            elif isinstance(item, dict):
                code = item.get("basis")
                meaning = item.get("meaning")
                if not isinstance(meaning, str) or not meaning.strip():
                    raise ValueError(
                        f"mapping basis policy {field} entries must include a non-empty meaning"
                    )
            else:
                code = None
            if not isinstance(code, str) or not code.strip():
                raise ValueError(
                    f"mapping basis policy {field} entries must be non-empty strings "
                    "or objects with a basis and meaning"
                )
            codes.append(code.strip())
        if len(codes) != len(set(codes)):
            raise ValueError(f"mapping basis policy {field} contains duplicate bases")
        return codes

    allowed = basis_codes(payload["allowed"], "allowed", required=True)
    denied = basis_codes(payload["denied"], "denied", required=False)
    if set(allowed) & set(denied):
        raise ValueError("mapping basis cannot be both allowed and denied")
    normalized_payload = dict(payload)
    normalized_payload["allowed"] = allowed
    normalized_payload["denied"] = denied
    return normalized_payload


def load_mapping_basis_policy(path: str | None = None) -> dict[str, object]:
    """读取策略；正式实验可绑定运行包内的冻结策略。

    显式参数优先于 ``CROSS_SOURCE_MAPPING_POLICY_PATH``；未设置环境变量时
    保持读取项目默认策略的既有行为。
    """
    configured_path = path or os.environ.get("CROSS_SOURCE_MAPPING_POLICY_PATH")
    policy_path = Path(configured_path).expanduser() if configured_path else _DEFAULT_MAPPING_POLICY_PATH
    return _load_mapping_basis_policy(str(policy_path.resolve(strict=True)))


def mapping_basis_status(value: object, *, policy_path: str | None = None) -> MappingBasisStatus:
    """返回依据状态；未列入规则的值保留为 undefined，不冒充 denied。"""
    normalized = str(value).strip() if isinstance(value, str) else ""
    policy = load_mapping_basis_policy(policy_path)
    if normalized in policy["allowed"]:
        return "allowed"
    if normalized in policy["denied"]:
        return "denied"
    return "undefined"


def allowed_mapping_bases(*, policy_path: str | None = None) -> frozenset[str]:
    policy = load_mapping_basis_policy(policy_path)
    return frozenset(str(item) for item in policy["allowed"])


def is_active_contract_status(value: object) -> bool:
    """Recognize the canonical code and explicit Chinese equivalent for active."""
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return normalized.casefold() in _ACTIVE_CONTRACT_STATUS_VALUES


def make_absence_check(
    table: str,
    filters: dict[str, str],
    row_count: int,
) -> dict[str, object]:
    """Build an absence-check record from executor-known query results."""
    if row_count < 0:
        raise ValueError("row_count must be non-negative")
    if not table or not filters or any(not key for key in filters):
        raise ValueError("absence check requires a table and explicit filters")
    return {
        "table": table,
        "filters": dict(filters),
        "row_count": row_count,
    }

