# -*- coding: utf-8 -*-
"""R1 / R2 抽取器开关（部署到 KAG 服务端的挂载点）

作用
----
让「是否启用我们改过的抽取器」由一个**标记文件**决定，不动上游源码、不改配置：

    标记文件不存在 → 本模块什么都不改，服务行为与本补丁上线前完全一致
    标记文件存在   → 把 `SchemaFreeExtractor._invoke` 换成我们的实现

为什么改成「替换类方法」，而不是「注册表同名覆盖」
--------------------------------------------------
最初的做法是 `ExtractorABC.register("schema_free_extractor")(我们的类)`。
但核对这台服务器后发现平台**未必按名字解析**：

  - 建库任务模板（`GET /public/v1/builder/job/get`）的 `extension.extractConfig`
    里只有 `llmPrompt / autoWrite / llm / autoSchema`，**没有抽取器类型**；
  - Java 外层 jar 的 574 个嵌套 jar 全量扫过，`schema_free_extractor` /
    `SchemaFreeExtractor` / `naive_rag_extractor` 等字符串**命中数为 0**；
  - `spg_server_bridge.run_component()` 用的是 `getattr(kag.interface, name)`，
    而 `kag.interface` 只导出 `ExtractorABC`，没有具体抽取器类。

也就是说：链配置是 Java 在内存里拼出来的，静态看不到，靠名字覆盖可能一辈子不触发。
**替换类方法则与「平台怎么拿到这个类」无关**——注册表、`getattr`、直接 import、
子类化，最终都调用同一个 `_invoke`，都会被拦到。

代价与边界
----------
- 只在**标记文件存在**时改，改的是内存里的类对象，**重启即恢复**（容器不重建）。
- 关闭状态下 `_invoke` 保持原对象不被替换，服务与上线前逐字相同。
- 若平台用的是另一个类（如 `KnowledgeUnitSchemaFreeExtractor`），本开关不生效——
  这是下一层实验要验证的事，不靠猜。

用法
----
启用：`mkdir -p /home/admin/kag_switch && touch /home/admin/kag_switch/r1_on`
关闭：`rm -f /home/admin/kag_switch/r1_on`
改完都要 `docker restart release-openspg-server`（pemja 内嵌解释器，模块级代码只在启动时跑一次）。

许可：Apache-2.0。
"""

from __future__ import annotations

import logging
import os
from collections import Counter

# 标记文件路径可用环境变量覆盖，便于本地自测；默认值即服务器上的部署路径。
MARKER = os.environ.get("KAG_R1_MARKER", "/home/admin/kag_switch/r1_on")

logger = logging.getLogger(__name__)

# 进程内是否已经打印过「首次命中」的日志，避免刷屏
_first_hit_logged = False


def enabled() -> bool:
    """开关是否打开（标记文件是否存在）。"""
    return os.path.exists(MARKER)


def _install() -> None:
    """把上游 `SchemaFreeExtractor` 的同步入口替换成我们的实现。"""
    global _first_hit_logged

    from kag.builder.component.extractor.schema_free_extractor import (
        SchemaFreeExtractor,
    )
    from kag.interface import ExtractorABC
    from kag_improve import AskDataSchemaFreeExtractor

    # R2 用的辅助方法挂到上游类上，使「非本类实例」也能调用
    SchemaFreeExtractor._reconcile_endpoints = (
        AskDataSchemaFreeExtractor._reconcile_endpoints
    )

    def _switched(self, input, **kwargs):
        global _first_hit_logged
        # 上游实例没有 stats，补一个，避免 R2 计数时报 AttributeError
        if not hasattr(self, "stats"):
            self.stats = Counter()
        if not _first_hit_logged:
            _first_hit_logged = True
            logger.warning("[R1R2] 首次命中：正在使用 R1/R2 抽取器处理文档")
        return AskDataSchemaFreeExtractor._invoke(self, input, **kwargs)

    _switched.__wrapped_original__ = SchemaFreeExtractor._invoke
    SchemaFreeExtractor._invoke = _switched

    # 同时保留注册表同名覆盖：若平台确实按名字解析，这条也生效（双保险）
    ExtractorABC.register("schema_free_extractor")(AskDataSchemaFreeExtractor)

    logger.warning(
        "[R1R2] 已接管 SchemaFreeExtractor._invoke -> %s（标记文件：%s）",
        AskDataSchemaFreeExtractor.__name__,
        MARKER,
    )


if enabled():
    _install()
else:
    logger.info("[R1R2] 未启用（标记文件 %s 不存在），使用上游原版抽取器", MARKER)


if __name__ == "__main__":
    from kag.builder.component.extractor.schema_free_extractor import (
        SchemaFreeExtractor,
    )

    print(f"marker     : {MARKER}")
    print(f"enabled    : {enabled()}")
    print(f"_invoke    : {SchemaFreeExtractor._invoke.__name__}")
