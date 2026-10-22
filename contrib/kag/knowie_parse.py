"""KAG 0.8.0 知识单元抽取结果的解析健壮化（R4）

背景：一次真实失败
------------------
2026-10-22 在本校服务器上用 KAG 0.8.0 的网页端「非结构化 + 混合索引」建库，
导入一篇某高校科研管理部门的立项公告（含 30 行项目名单表格）后，构建卡住不出图。日志：

    kag/builder/prompt/default/knowledge_unit.py:250  parse_response
      -> kag/builder/prompt/default/util.py:51
         raise ValueError("the output KnowUnit str is invalid: ...")
    json.decoder.JSONDecodeError: Invalid control character at: line 80 column 10 (char 3640)

同类错误在 7 分钟内出现 13 次。根因不是模型没答，而是模型在 JSON 字符串值里
写了一个**裸换行符**，而 `json.loads` 默认 `strict=True`，遇到字符串内部的
0x00-0x1F 控制字符直接拒绝。

上游的补救为什么不生效
----------------------
上游 `kag/builder/prompt/default/util.py:8-54` 的 `load_knowIE_data` 写了 5 层
fallback，逐层做的事是（行号为 fdab15b / 0.8.0 实测）：

    :10  json.loads(respond)
    :14-18  用 respond 里的首尾大括号截取 + 把 \\' 换成 '
    :21-29  重新拼大括号 + 用英文键正则修 `"Content": "..."` 里的引号
    :32-42  剥 ```json 围栏 / output: 前缀
    :49  在末尾补一个 `}` 再解析
    :51  全部失败后 `raise ValueError("the output KnowUnit str is invalid: ")`

**没有一层处理字符串内的裸控制字符**。这不是推测：
在整个 KAG 0.8.0 源码树里搜索 `strict=`，命中数 **0**；搜索
`control char` / `0x1f` / `unescape_control` 之类的处理，命中数也是 **0**。
所以含裸换行的输出会让 5 层全部落空，最后在 :51 抛错。

两处附带问题（同一函数内，可单独提）：

    :14-16   left_pos / right_pos 在 `respond` 上计算，却被用来切
             `extract_ret_str`——而后者是 `:12` 经 `modify_knowledge_unit`
             改写过、长度已变的字符串（`:160-167` 会把
             `"知识点1名称": "xxx",` 改写成 `"xxx":`）。
             两者长度不等时切片位置是错的。
    :23      `(?<="Content": ")(.*?)(?=",\\n    "Knowledge Type")` 只匹配英文键；
             中文提示词下键名是「内容」「知识类型」，这条正则永不命中。

修复只改一个参数，不动上面任何一层。

修复
----
`json.loads(s, strict=False)`。Python 文档对该参数的定义是「允许字符串内部出现
控制字符」，除此之外不放松任何语法。所以它不是"吞掉所有错误"，缺失括号、
多余逗号、非法转义仍然会抛错——本文件的测试里专门验证了这一点。

调用点在 `kag/builder/prompt/default/knowledge_unit.py:247` 的 `parse_response`，
`:250` 的 `rsp = load_knowIE_data(response)`。也就是说这一处一改，
「非结构化 + 混合索引」建库时所有知识单元抽取的解析都跟着修好。

这个文件把上游的 5 层尝试顺序原样复刻出来，并提供一个 `strict` 开关，
让评审能在一台没装 KAG 的机器上直接看到「同一份输入，修不修差在哪里」。

依赖：仅标准库。
许可：Apache-2.0。
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Tuple

# ---------------------------------------------------------------------------
# 上游 util.py 里的几个字面量，原样保留，便于对照
# ---------------------------------------------------------------------------
_JSON_FENCE = "```json"
_FENCE = "```"
_OUTPUT_TAG = "output:\n"

# util.py:23 的英文键正则，原样保留（中文输出下不匹配任何内容）
_UPSTREAM_KEY_FIX = re.compile(r'(?<="Content": ")(.*?)(?=",\n    "Knowledge Type")')

# 上游抛错时使用的固定前缀（util.py:51-52）
_UPSTREAM_ERR_PREFIX = "the output KnowUnit str is invalid: "


# ---------------------------------------------------------------------------
# 上游的 5 层补救，逐层拆成纯函数，方便单独验证「它们都没有碰控制字符」
# ---------------------------------------------------------------------------
def layer2_braces(respond: str) -> str:
    """上游第 2 层：按首尾大括号截取，并把 `\\'` 还原成 `'`。"""
    left = respond.find("{") if respond.find("{") >= 0 else 0
    right = respond.rfind("}") + 1
    return respond[left:right].strip().replace("\\'", "'")


def layer3_rebuild(respond: str) -> str:
    """上游第 3 层：重拼大括号 + 修英文 Content 字段里的引号。"""
    text = "{" + "{".join(respond.split("{")[1:])
    text = text.split("}\n}")[0] + "}\n}"
    return _UPSTREAM_KEY_FIX.sub(lambda m: m.group(1).replace('"', r"\""), text)


def layer4_fence(respond: str) -> str:
    """上游第 4/5 层：剥掉 markdown 围栏与 output: 前缀。"""
    text = respond
    if _JSON_FENCE in text:
        text = text.split(_JSON_FENCE)[1]
    if _OUTPUT_TAG in text:
        text = text.split(_OUTPUT_TAG)[1]
    if _FENCE in text:
        text = text.split(_FENCE)[0]
    else:
        text = text.replace("\\'", "'").replace('\\"', '"')
    return text


def attempt_chain(respond: str, strict: bool = True) -> Tuple[Any, int]:
    """按上游 util.py:8-54 的顺序尝试解析，返回 (结果, 第几层成功)。

    参数
    ----
    strict : 传给每一次 `json.loads` 的 strict 参数。
        `True`  = 上游现状（字符串内裸控制字符即失败）
        `False` = 本项目的修复

    异常
    ----
    5 层全部失败时抛 `ValueError`，消息前缀与上游一致。
    """
    try:
        return json.loads(respond, strict=strict), 1
    except Exception:
        pass
    try:
        return json.loads(layer2_braces(respond), strict=strict), 2
    except Exception:
        pass
    try:
        return json.loads(layer3_rebuild(respond), strict=strict), 3
    except Exception:
        pass
    try:
        return json.loads(layer4_fence(respond), strict=strict), 4
    except Exception:
        pass
    try:
        return json.loads(layer4_fence(respond) + "}", strict=strict), 5
    except Exception:
        raise ValueError(_UPSTREAM_ERR_PREFIX + respond[:400])


def upstream_parse(respond: str) -> Any:
    """上游现状的等效调用（strict=True）。含裸控制字符时必然抛 ValueError。"""
    return attempt_chain(respond, strict=True)[0]


def load_knowie_data(respond: str, strict: bool = False) -> Any:
    """修复版：与上游同一套尝试顺序，但 `json.loads` 传 `strict=False`。

    `strict=False` 只允许字符串内部出现控制字符，其余 JSON 语法照旧校验。
    """
    return attempt_chain(respond, strict=strict)[0]


# ---------------------------------------------------------------------------
# 诊断工具：定位裸控制字符到底出现在哪儿
# ---------------------------------------------------------------------------
def scan_bare_controls(text: str) -> List[int]:
    """返回「字符串字面量内部」的裸控制字符下标列表。

    只统计未被反斜杠转义的 0x00-0x1F。用于证明「上游 5 层补救都没有消除它」。
    """
    positions: List[int] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif ord(char) < 0x20:
                positions.append(index)
        elif char == '"':
            in_string = True
    return positions


def describe_failure(respond: str) -> str:
    """给出一段可直接贴进报告的失败描述。"""
    hits = scan_bare_controls(respond)
    if not hits:
        return "输入中未发现字符串内的裸控制字符"
    first = hits[0]
    line = respond.count("\n", 0, first) + 1
    column = first - (respond.rfind("\n", 0, first) + 1) + 1
    return (
        f"字符串内裸控制字符 {len(hits)} 处；"
        f"首个位于 line {line} column {column} (char {first})"
    )
