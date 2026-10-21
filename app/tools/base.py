"""工具的抽象基类与元信息。

一个「工具」= 一段能回答某类问题的能力。系统里有三类：

    nl2sql       结构化查询 —— 拿问题生成 SQL、查业务表、回结果行
    doc_search   文档检索 —— 拿问题在语料里找相关段落
    graph_reason 图谱推理 —— 拿问题在知识图谱上多跳行走，连带回原文

**三者共用同一个签名**（契约定义，不可改）：

    tool(input) -> ToolResult(data, trace, refs)

本模块只负责「统一形状」和「自描述」，不关心工具内部怎么实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.contracts import ToolResult

#: 三类能力的标识。编排层的路由规则、工具注册表的键都用它。
CAPABILITY_TABLE = "structured_table"
CAPABILITY_DOC = "unstructured_doc"
CAPABILITY_GRAPH = "knowledge_graph"


@dataclass(frozen=True)
class ToolSpec:
    """工具的「名片」——回答「它是谁、能干什么、要什么入参」。

    存在两个用途，缺一不可：

    1. **给人（编排层路由用）**：`description` + `capabilities` 让规则式路由能
       按关键词判断该调谁。
    2. **给模型（规划用）**：当规则式路由置信度不足、需要升级成模型规划时，
       这张名片就是要塞进提示词的工具清单。

    Attributes:
        name: 工具的唯一标识，会写进 `TraceStep.tool`（所以改这个值等于改所有历史记录）。
        capabilities: 能覆盖哪几类能力，取值见本模块 `CAPABILITY_*` 常量。
        description: 一句人话说明「什么时候该用它」。会进模型提示词。
        input_schema: 入参形状（字段名 -> 一句人话说明）。
        returns: 产出是什么形态的人话说明。
        cost_hint: 相对代价提示（快 / 慢），供规划时权衡。
    """

    name: str
    capabilities: tuple[str, ...]
    description: str
    input_schema: dict[str, str] = field(default_factory=dict)
    returns: str = ""
    cost_hint: str = "中"


class BaseTool(ABC):
    """所有工具的共同父类。

    子类只需要实现 `spec`（名片）和 `run`（干活）。不许在 `run` 里直接
    返回裸数据 —— 必须包成 `ToolResult`，因为下游（编排层合并 trace、
    `collect_refs()` 去重、前端画引用卡片）全都依赖 trace / refs。

    ⚠️ 最容易犯的错：mock 只返回 data 就交差。
       那样等于没测到编排逻辑（编排层要处理的恰恰是「多条 trace 怎么拼、
       重复 refs 怎么去重」），验证期结束换真实现时才会发现接不上。
    """

    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        """返回工具名片。"""
        raise NotImplementedError

    @abstractmethod
    def run(self, payload: dict[str, Any]) -> ToolResult:
        """执行工具。

        Args:
            payload: 工具入参，形状由 `spec.input_schema` 声明。

        Returns:
            `ToolResult`，三个字段都要填：`data`（原始产出）、
            `trace`（内部步骤）、`refs`（引用列表）。
        """
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self.spec.name
