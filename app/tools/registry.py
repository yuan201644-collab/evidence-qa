"""工具注册表：编排层从这里拿「有哪些工具可用」。

为什么要有注册表，而不是在编排层里硬编码几个 if：
    ① 编排层要**按名字**找工具（模型规划后说「调 graph_reason」，得有地方查）
    ② 编排层要**按能力**找工具（「这个问题要图谱能力」，得有地方筛）
    ③ 换实现不改调用方 —— 验证期是 mock、之后是 KAG，编排层代码一行不用动

**放置原则**：注册表属于编排层的一部分，与「工具真假」无关。
    所以 Mock A 能测到它，KAG 接进来也不用改。
"""

from __future__ import annotations

from app.tools.base import BaseTool


class ToolRegistry:
    """工具登记处。登记、按名查、按能力筛。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """登记一个工具。重名直接报错——重名会让路由变成随机行为。"""
        name = tool.name
        if name in self._tools:
            raise ValueError("工具名重复：" + name)
        self._tools[name] = tool

    def get(self, name: str) -> BaseTool | None:
        """按名字取工具，没有则返回 None（不抛异常，方便路由试探）。"""
        return self._tools.get(name)

    def require(self, name: str) -> BaseTool:
        """按名字取工具，没有就报错。用于「路由已经决定要用它」的场合。"""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError("工具未注册：" + name)
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def find_by_capability(self, capability: str) -> list[BaseTool]:
        """按能力筛工具，返回所有覆盖该能力的工具。"""
        found = []
        for tool in self._tools.values():
            if capability in tool.spec.capabilities:
                found.append(tool)
        return found

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def describe_all(self) -> list[dict]:
        """把全部工具名片导出成普通字典。

        用途：升级成「模型规划」时，这份清单就是要塞进提示词的内容。
        现在规则式路由用不上，但先备好 —— 否则 09-24 放开模型规划时又要返工。
        """
        out = []
        for name in self.names():
            spec = self._tools[name].spec
            out.append(
                {
                    "name": spec.name,
                    "capabilities": list(spec.capabilities),
                    "description": spec.description,
                    "input_schema": dict(spec.input_schema),
                    "returns": spec.returns,
                    "cost_hint": spec.cost_hint,
                }
            )
        return out
