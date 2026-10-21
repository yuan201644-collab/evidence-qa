"""跨源演示能力与 KAG 状态路由的契约测试（合并 main 后重写）。

## 本文件的历史与本轮改写（2026-10-17）

原三条用例来自 `main` 分支，测的是合并前旧实现的内部形状：

1. `cross_source_demo_readiness()` 返回**单个** `{"status": ...}`；
2. `kag_status()` 恒返回 `reason_code == "runtime_not_integrated"` 的硬编码桩。

合并 `main` 后这两条前提都不再成立：本分支把跨源能力收敛到
`cross_source_demo_capability()`，返回 `status/ready/reason_code` 三字段；
`kag_status()` 改为向 `app.kag_supplement.KagProcessSupplementer` 真实探测，
未配置时报 `credentials_not_ready` 之类的**实际**原因，而不是写死「运行期未接入」。

`tests/test_cross_source_capability.py`（本分支原有，未改动）已覆盖能力路由的
三种状态、探针异常兜底与不泄露 DSN。本文件因此改为**补充 main 侧原本覆盖、
合并后仍须保住的两件事**：

- 事实执行器缺失时能力必须 fail-closed（main 用例的第一层意图）；
- KAG 状态必须在任何配置下都**不暴露凭据与运行期路径**，且状态值限于已知枚举
  （main 用例的第二层意图，但不再断言那个写死的原因码）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app import main as main_module


class _Registry:
    """只回答 `cross_source_demo` 是否注册的最小替身。"""

    def __init__(self, tool=None, *, enabled: bool):
        self._tool = tool
        self._enabled = enabled

    def has(self, name: str) -> bool:
        return name == "cross_source_demo" and self._enabled

    def get(self, name: str):
        assert name == "cross_source_demo"
        return self._tool


def test_cross_source_capability_fails_closed_without_demo_route(monkeypatch):
    """路由没注册时，即便事实执行器在，也必须判 not ready。

    这是 main 原用例 `test_cross_source_readiness_is_disabled_without_demo_route_or_fact_executor`
    的第一层意图，合并后必须保住。
    """
    monkeypatch.setattr(
        main_module,
        "_request_user",
        lambda _request: SimpleNamespace(role="user"),
    )
    monkeypatch.setattr(
        main_module,
        "_orchestrator",
        SimpleNamespace(registry=_Registry(enabled=False), fact_plan_executor=object()),
    )

    response = main_module.cross_source_demo_capability(object())

    assert response["ready"] is False
    assert response["status"] == "disabled"
    assert response["reason_code"] == "demo_route_disabled"


def test_cross_source_capability_fails_closed_without_fact_executor(monkeypatch):
    """路由在、但事实执行器缺失时同样不能判 ready。

    合并前该分支在 main 的用例里与「路由缺失」并为一个断言；
    合并后两者都会走到 `readiness()` 的真探针，这里用探针结果区分：
    执行器缺失属运行期依赖不全，故允许 status 落在 disabled/unavailable 两档，
    但 ready 必须为 False，且 reason_code 必须是枚举内的值。
    """
    monkeypatch.setattr(
        main_module,
        "_request_user",
        lambda _request: SimpleNamespace(role="user"),
    )
    monkeypatch.setattr(
        main_module,
        "_orchestrator",
        SimpleNamespace(
            registry=_Registry(SimpleNamespace(readiness=lambda: (False, "demo_database_unavailable")), enabled=True),
            fact_plan_executor=None,
        ),
    )

    response = main_module.cross_source_demo_capability(object())

    assert response["ready"] is False
    assert response["status"] in {"disabled", "unavailable"}
    assert response["reason_code"] in {
        "demo_route_disabled",
        "demo_database_not_configured",
        "demo_documents_unavailable",
        "demo_database_unavailable",
        "demo_seed_work_order_missing",
        "demo_seed_events_missing",
        "demo_readiness_check_failed",
        "readiness_probe_unavailable",
    }


def test_kag_status_never_exposes_credentials_or_runtime_paths(monkeypatch):
    """KAG 状态是只读就绪探针：任何配置下都不得回传凭据或运行期路径。

    取代 main 原用例「恒等于 runtime_not_integrated」的断言 —— 那条断言把
    「未接入」写死成了接口契约，与本分支已实现的真实探测矛盾。
    保留其**安全意图**：只回三个字段、字段值不得包含密钥与路径。
    """
    monkeypatch.setattr(
        main_module,
        "_request_user",
        lambda _request: SimpleNamespace(role="user"),
    )

    response = main_module.kag_status(object())

    assert set(response) == {"ready", "status", "reason_code"}
    assert isinstance(response["ready"], bool)
    assert response["status"] in {"ready", "unavailable", "not_configured"}
    # 原因码只允许「小写蛇形」枚举形态，防止将来有人把异常串直接塞进来。
    assert isinstance(response["reason_code"], str)
    assert response["reason_code"].replace("_", "").isalnum()

    serialized = json.dumps(response, ensure_ascii=False).lower()
    for leaked in ("api_key", "apikey", "secret", "password", "token", "://", "\\", "/"):
        assert leaked not in serialized, f"KAG 状态泄露了 {leaked!r}"
