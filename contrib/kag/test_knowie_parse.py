"""R4 自测：KAG 知识单元解析对「字符串内裸控制字符」的健壮性

不需要 KAG、不需要外网、不需要模型。直接运行：

    python test_knowie_parse.py

设计原则：既能证明「修复有效」，也要证明「修复是窄的」——
`strict=False` 只放行字符串内的控制字符，其余 JSON 语法照旧校验。
否则就变成了"把异常吞掉"，那比原缺陷更糟。
"""

from __future__ import annotations

import json
import os

from knowie_parse import (
    attempt_chain,
    describe_failure,
    layer2_braces,
    layer3_rebuild,
    layer4_fence,
    load_knowie_data,
    scan_bare_controls,
    upstream_parse,
)

PASSED = 0


def check(name: str, condition: bool) -> None:
    global PASSED
    if not condition:
        raise AssertionError(name)
    PASSED += 1
    print(f"  PASS  {name}")


def raises(function, *args, **kwargs) -> bool:
    try:
        function(*args, **kwargs)
    except Exception:
        return True
    return False


def raises_json_decode(function, *args) -> bool:
    try:
        function(*args)
    except json.JSONDecodeError:
        return True
    except Exception:
        return False
    return False


def error_text(function, *args) -> str:
    try:
        function(*args)
    except Exception as exc:
        return str(exc)
    return ""


# ---------------------------------------------------------------------------
# 失败样本：形状取自 2026-10-22 服务器上那次真实失败
# 真实日志：ValueError: the output KnowUnit str is invalid ...
#           JSONDecodeError: Invalid control character at: line 80 column 10 (char 3640)
# 这里保留「字符串值内部出现一个裸换行」这一个决定性特征，正文取自
# 同一篇公告（高校科研管理部门的立项名单，公开信息），篇幅缩短。
# ---------------------------------------------------------------------------
BROKEN = """{
  "2024年度江苏高校哲学社会科学研究一般项目立项": {
    "内容": "根据《江苏省教育厅办公室关于公布2024年度江苏高校哲学社会科学研究
一般项目的通知》（苏教办社政函〔2024〕16号），我校共获得一般项目20项。",
    "知识类型": "政策文件"
  },
  "立项项目由学校给予经费资助": {
    "内容": "学校对立项的一般项目给予每项1万元的经费资助。",
    "知识类型": "资助标准"
  }
}"""

EXPECTED_CONTENT = (
    "根据《江苏省教育厅办公室关于公布2024年度江苏高校哲学社会科学研究\n"
    "一般项目的通知》（苏教办社政函〔2024〕16号），我校共获得一般项目20项。"
)

VALID = '{"甲": {"内容": "无换行的一段话", "知识类型": "政策文件"}}'


print("[1] 语言事实：裸换行让 json.loads 默认失败，strict=False 放行")
check("修复前 json.loads 抛错", raises(json.loads, BROKEN))
check("抛的确实是 JSONDecodeError", raises_json_decode(json.loads, BROKEN))
check("异常消息指出是控制字符", "Invalid control character" in error_text(json.loads, BROKEN))
check("strict=False 可以解析", isinstance(json.loads(BROKEN, strict=False), dict))
check(
    "解析后换行被保留",
    json.loads(BROKEN, strict=False)["2024年度江苏高校哲学社会科学研究一般项目立项"]["内容"]
    == EXPECTED_CONTENT,
)

print("[2] 上游 5 层补救都不处理字符串内控制字符")
check("样本中确实只有 1 处裸控制字符", len(scan_bare_controls(BROKEN)) == 1)
check("第2层（截括号+换 \\'）后裸字符仍在", len(scan_bare_controls(layer2_braces(BROKEN))) == 1)
check("第3层（重拼括号+修英文键）后裸字符仍在", len(scan_bare_controls(layer3_rebuild(BROKEN))) == 1)
check("第4层（剥围栏）后裸字符仍在", len(scan_bare_controls(layer4_fence(BROKEN))) == 1)
check("上游 strict=True 全链路抛 ValueError", raises(upstream_parse, BROKEN))
check(
    "错误消息前缀与上游 util.py:51 一致",
    "the output KnowUnit str is invalid:" in error_text(upstream_parse, BROKEN),
)

print("[3] 修复后同一输入一次通过")
result, layer = attempt_chain(BROKEN, strict=False)
check("解析成功", isinstance(result, dict))
check("第一层就成功（不是靠补括号兜底）", layer == 1)
check("两条知识单元都在", len(result) == 2)
check("内容字段与预期逐字相同", result["2024年度江苏高校哲学社会科学研究一般项目立项"]["内容"] == EXPECTED_CONTENT)
check("修复对正常 JSON 无副作用", load_knowie_data(VALID) == json.loads(VALID))

print("[4] 修复是窄的：除控制字符外，行为与上游逐条一致")
check("缺结尾括号：上游/修复都可解析（第5层补 } 是既有兜底）", raises(upstream_parse, '{"a": {"b": 1}') is False)
check("缺结尾括号：修复后结果与上游相同", load_knowie_data('{"a": {"b": 1}') == upstream_parse('{"a": {"b": 1}'))
check("多余逗号仍抛错", raises(load_knowie_data, '{"a": 1,}'))
check("非法转义 \\x 仍抛错", raises(load_knowie_data, '{"a": "bad \\x escape"}'))
check("值内裸双引号仍抛错", raises(load_knowie_data, '{"a": "he said "hi""}'))
check("纯文本仍抛错", raises(load_knowie_data, "抱歉，我无法完成这个任务。"))
check("空串仍抛错", raises(load_knowie_data, ""))
check("无效输入在修复下抛的仍是 ValueError", raises(load_knowie_data, "not json at all"))

print("[5] 其他控制字符同样适用；诊断信息可用")
check("制表符 \\t 可修", load_knowie_data('{"a": "x\ty"}') == {"a": "x\ty"})
check("垂直制表符 \\x0b 可修", load_knowie_data('{"a": "x\x0by"}') == {"a": "x\x0by"})
check("字符串内部无控制字符时不算失败", len(scan_bare_controls(VALID)) == 0)
check("describe_failure 能给出行列", "line 3" in describe_failure(BROKEN))
check("describe_failure 对干净输入给出明确结论", "未发现" in describe_failure(VALID))

# ---------------------------------------------------------------------------
# [6] 可选：拿**真实的上游 util.py** 跑一遍
# 设置环境变量 KAG_UTIL_PY 指向上游 kag/builder/prompt/default/util.py 即启用。
# 这一段不复制上游源码，只在内存里 exec 一次原始版本和打过补丁的版本，
# 避免把别人的文件塞进我们的仓库。
# ---------------------------------------------------------------------------
_UPSTREAM_UTIL = os.environ.get("KAG_UTIL_PY", "").strip()

if not _UPSTREAM_UTIL:
    print("[6] 跳过（未设置 KAG_UTIL_PY）——真实上游函数对比")
else:
    print(f"[6] 用真实上游函数对比：{_UPSTREAM_UTIL}")
    print("    （下面 stderr 若出现上游 logger 的 traceback 属预期：")
    print("      那是 5 层重试各自失败的现场，正是缺陷证据本身）")

    def _exec(source: str, name: str) -> dict:
        namespace: dict = {"__name__": name}
        exec(compile(source, _UPSTREAM_UTIL, "exec"), namespace)
        return namespace

    with open(_UPSTREAM_UTIL, encoding="utf-8") as handle:
        _original_source = handle.read()

    _patched_source = _original_source
    for _old, _new in (
        ("json.loads(respond)", "json.loads(respond, strict=False)"),
        ("json.loads(extract_ret_str)", "json.loads(extract_ret_str, strict=False)"),
        ("json.loads(extract_ret)", "json.loads(extract_ret, strict=False)"),
        ('json.loads(extract_ret_str + "}")', 'json.loads(extract_ret_str + "}", strict=False)'),
    ):
        _patched_source = _patched_source.replace(_old, _new)

    check("补丁确实改到了源码里的 json.loads", _patched_source != _original_source)
    check("原版源码里没有 strict= 字样", "strict=" not in _original_source)

    original_ns = _exec(_original_source, "kag_util_original")
    patched_ns = _exec(_patched_source, "kag_util_patched")

    check(
        "真实上游 load_knowIE_data 对同一输入抛 ValueError",
        raises(original_ns["load_knowIE_data"], BROKEN),
    )
    check(
        "真实上游抛的正是那条消息",
        "the output KnowUnit str is invalid:" in error_text(original_ns["load_knowIE_data"], BROKEN),
    )
    patched_result = patched_ns["load_knowIE_data"](BROKEN)
    check("打过补丁的真实上游函数能解析", isinstance(patched_result, dict))
    check("解析出两条知识单元", len(patched_result) == 2)
    check("内容与我们的复刻结果逐字一致", patched_result == load_knowie_data(BROKEN))
    check(
        "正常 JSON 在补丁后结果不变",
        patched_ns["load_knowIE_data"](VALID) == original_ns["load_knowIE_data"](VALID),
    )

print(f"\nALL CHECKS PASSED  ({PASSED} 项)")
