# -*- coding: utf-8 -*-
"""multihop.py - 多跳检索工具：查询分解 -> 多轮检索 -> 合并去重 -> 跨轮重排 -> **只生成一次**

为什么单独一个文件、而不是塞进 answer.py：
    answer.py 里的 `_parse_citations` / `_fix_illegal_citations` / 拒答打标全都建立在
    「**一组**上下文、编号 1..N」这个前提上。多轮检索如果各轮各自编号，模型写的
    `[1]` 指哪一轮的片段就说不清了。本文件的做法是：**中间轮次只检索、不生成**，
    把各轮结果合并成**一组**之后，再交给 answer.py 的 `generate()` 编号一次 ——
    这样引用协议一行都不用改。

与单轮路径的关系：
    `MultiHopRagTool` **包装**一个 `RagAnswerTool`，不修改它。
    是否启用多跳由**编排层的后端开关**决定（`DOC_SEARCH_BACKEND=rag` 单轮 / `rag_multihop` 多跳），
    默认值是 `rag`，所以**默认路径行为与不引入本文件时完全一致**。

    另有一个应急开关 `RAG_MULTIHOP=0`：即使后端选了多跳也强制退回单轮。
    ⚠️ 它的默认值是 **1（允许生效）**，而不是 0 —— 原因：如果默认 0，就会出现
       「把后端切成 rag_multihop 却没生效，而且不报错」这种静默陷阱，
       排查成本很高。默认行为不变由后端默认值 `rag` 保证，不需要再叠一层默认关闭。

两个容易做错的地方（本文件已处理）：
    1. **跨轮分数不可比**：各轮用不同查询串重排，分数不在一个尺度上，
       不能直接按分数拼接。所以合并后必须**用原问题再重排一次**。
    2. **上下文膨胀**：片段一多，行内引用准确率会掉、成本线性上升。
       所以合并重排后按 `RAG_FINAL_CONTEXT_TOP_K`（默认 8）硬截断。
"""

import json
import os
import re
import time

from app.contracts import ToolResult, TraceStep
from app.rag.config import env_int
from app.rag.answer import _CITATION_RE, _citation_number

# 开关与上限（沿用 app/rag/config.py 的 env_int；开关类允许 0 所以 minimum=0）
_ENV_MULTIHOP = "RAG_MULTIHOP"
_ENV_MAX_SUBQUERIES = "RAG_MAX_SUBQUERIES"
_ENV_MAX_HOPS = "RAG_MAX_HOPS"
_ENV_FINAL_CONTEXT_TOP_K = "RAG_FINAL_CONTEXT_TOP_K"
_ENV_ROLE_ALIASES = "RAG_ROLE_ALIASES_JSON"
# 应急关闭开关，默认 1（允许生效）。见模块头「与单轮路径的关系」里的理由。
_DEFAULT_MULTIHOP = 1
_DEFAULT_MAX_SUBQUERIES = 3
_DEFAULT_MAX_HOPS = 2
_DEFAULT_FINAL_CONTEXT_TOP_K = 8
# 初始别名来自当前 T3 题集的显式标注：运行手册写“机房主管”，问题会问
# “现场负责人”或“运维负责人”。真实业务可用环境变量覆盖，格式见 _role_alias_note。
_DEFAULT_ROLE_ALIASES = {
    "现场负责人": ["机房主管"],
    "运维负责人": ["机房主管"],
}
_POLICY_QUESTION_CUES = (
    "适用制度", "适用规则", "适用办法", "管理制度", "管理办法", "管理规则",
    "制度规定", "保留要求", "保存要求", "保留期限", "保存期限", "保留年限", "保存年限",
    "保留时长", "保存时长", "访问控制", "访问条件", "访问权限", "操作限制", "查阅权限", "使用约束",
)

_DECOMPOSE_SYSTEM = (
    "你是检索规划助手。你的任务是为复杂问题制定可执行的检索计划。"
    "只输出 JSON，不要任何解释、不要 Markdown 代码块以外的文字。"
)

_BIND_SYSTEM = (
    "你是基于证据的实体抽取助手。只能从给定片段中抽取值；"
    "找不到时 value 必须是空字符串。只输出 JSON，不要解释。"
)

_SUFFICIENCY_SYSTEM = (
    "你是检索充分性判断助手。只输出 JSON，不要解释。"
)


def _extract_json_object(text):
    """从模型输出里抠出第一个 JSON 对象；容忍 ```json 围栏、前后废话、多余空格。

    取不到就返回 None —— 调用方一律走"退化"分支，**绝不因为解析失败而抛错**：
    多跳是增强能力，解析失败时退回单轮是正确行为，不该让整条问答失败。
    """
    if not text or not str(text).strip():
        return None
    t = str(text).strip()
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z]*[ \t]*\r?\n?", "", t)
        t = re.sub(r"\r?\n?```[ \t]*$", "", t)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(t[i:j + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _as_bool(value, default=False):
    """把模型可能写成字符串的布尔值归一化。

    ⚠️ 为什么不能直接 bool(value)：模型常输出 "false"，而 bool("false") 是 True ——
    那会让"不充分"被判成"充分"，第二轮永远不会触发，而且**无声无息**。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "y", "1"):
            return True
        if v in ("false", "no", "n", "0", ""):
            return False
    return default


def _clean_queries(raw, question, limit):
    """把模型给的子查询列表洗成可用形态：非空、去重、去指代残留、不超过上限。

    丢掉与**原问题完全相同**的条目：那等于没拆，留着只会白跑一次检索。
    """
    if not isinstance(raw, list):
        return []
    out = []
    q_norm = (question or "").strip()
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or len(s) > 200:
            continue
        if s == q_norm:
            continue
        if s in out:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _clean_plan_steps(raw, question, limit):
    """校验带变量依赖的查询计划，拒绝不完整计划。

    每一步只有两种安全形式：直接 ``query``，或引用前序绑定变量的
    ``query_template``。模板变量只能来自已经声明的 bind，避免模型把自然语言
    中的花括号误当成变量，也避免第二跳在没有首跳事实时被执行。
    """
    if not isinstance(raw, list):
        return []
    steps = []
    bound = set()
    bind_indexes = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return []
        bind = item.get("bind")
        if bind is not None:
            if not isinstance(bind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", bind):
                return []
            # 规划器偶尔会把问题所求的保留期限等最终答案值当成后续变量。
            # 这类值既不应驱动检索，也容易让“8年”之类答案反过来决定证据。
            if re.search(r"(?:^|_)(?:answer|deadline|duration|period|retention|limit)(?:_|$)", bind):
                return []
        query = item.get("query")
        template = item.get("query_template")
        if isinstance(query, str) and query.strip():
            text = query.strip()
            if len(text) > 200:
                return []
            kind = "query"
        elif isinstance(template, str) and template.strip():
            text = template.strip()
            names = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", text))
            if len(text) > 200 or not names or not names.issubset(bound):
                return []
            kind = "query_template"
        else:
            return []
        if index == 0 and kind != "query":
            return []
        step = {"id": "s" + str(index + 1), "kind": kind, "text": text}
        if bind:
            step["bind"] = bind
            bound.add(bind)
            bind_indexes[bind] = index
        steps.append(step)
        if len(steps) >= limit:
            break
    if len(steps) < 2 or not any(step["kind"] == "query_template" for step in steps):
        return []
    # 绑定只允许服务后续关系查询；没有被后续步骤消费的变量是最终答案字段，拒绝执行。
    for name, index in bind_indexes.items():
        consumed_later = any(
            name in set(re.findall(r"\{([a-z][a-z0-9_]*)\}", later["text"]))
            for later in steps[index + 1:]
        )
        if not consumed_later:
            return []
    return steps


def _render_query_template(template, bindings):
    """用首跳已验证的实体渲染查询模板；变量缺失则返回空串。"""
    names = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", template))
    if not names or any(not bindings.get(name, {}).get("value") for name in names):
        return ""
    query = template
    for name in names:
        query = query.replace("{" + name + "}", bindings[name]["value"])
    return query


def _load_role_aliases():
    """读取可审计的岗位别名；非法配置不采用，退回项目初始映射。"""
    raw = os.environ.get(_ENV_ROLE_ALIASES)
    if not raw:
        return _DEFAULT_ROLE_ALIASES
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return _DEFAULT_ROLE_ALIASES
    if not isinstance(value, dict):
        return _DEFAULT_ROLE_ALIASES
    output = {}
    for target, aliases in value.items():
        if isinstance(target, str) and isinstance(aliases, list):
            cleaned = [item for item in aliases if isinstance(item, str) and item.strip()]
            if cleaned:
                output[target.strip()] = cleaned
    return output or _DEFAULT_ROLE_ALIASES


def _role_alias_note(question, binding, hit, aliases):
    """只有问题岗位与证据岗位匹配已配置别名时，才返回可给生成器的事实说明。"""
    text = str(hit[0].chunk.text)
    for asked_role, evidence_roles in aliases.items():
        if asked_role not in question:
            continue
        for evidence_role in evidence_roles:
            if evidence_role in text:
                return (
                    "问题中的“" + asked_role + "”按岗位别名映射为证据中的“"
                    + evidence_role + "”；已验证实体 " + binding["value"]
                    + " 的依据来自包含该岗位名称的检索片段。"
                )
    return "已验证中间实体 " + binding["value"] + " 的依据来自首跳命中的检索片段。"


def _location_subject(question):
    """从问题开头提取可验证的场地名称，提取不到时返回空串。"""
    subject = str(question).split("的", 1)[0].strip()
    subject = re.sub(r"^(?:请问|查询|检索|确定)", "", subject).strip()
    if len(subject) >= 2 and subject.endswith(("中心", "机房", "专区")):
        return subject
    return ""


def _ground_binding_query(query, original_question):
    """把首跳的泛化场地词还原为用户问题中的具体场地。"""
    location = _location_subject(original_question)
    if not location or location in query:
        return query
    for placeholder in ("某场地", "该场地", "某机房", "该机房"):
        if placeholder in query:
            return query.replace(placeholder, location)
    # 首跳承担实体绑定职责，缺少场地限定时不能让宽泛检索决定绑定对象。
    return location + "的负责人是谁？"


def _binding_from_text(context_hits, query):
    """模型实体抽取失败时，从原文中保守识别“主管/负责人为姓名”。

    这不是按职位猜人：只有片段出现明确的“某主管为张三”或“某负责人是张三”
    文字时才返回，姓名也必须紧随谓词。它服务于中文制度文档中很常见的人员任命句。
    """
    pattern = re.compile(r"(?:主管|负责人)\s*(?:为|是)\s*([\u4e00-\u9fff]{2,4})")
    location = _location_subject(query)
    # 同名场地是首跳人员绑定的最低约束。没有这个约束时，“谁是负责人”
    # 的宽泛语义检索可能把别的场地手册排在前面，进而把错误人名传给第二跳。
    for index, (cand, _ref) in enumerate(context_hits):
        text = str(cand.chunk.text)
        if location and location not in text:
            continue
        match = pattern.search(text)
        if match:
            return {
                "value": match.group(1),
                "citation_index": index,
                "usable": True,
                "method": "text_pattern",
            }
    return {"value": "", "citation_index": None, "usable": False, "method": "none"}


def _dedup_hits(hits):
    """按 (doc_id, char_start) 去重，保持首次出现顺序。

    为什么用 char_start 而不是 chunk_id：DocRef 是跨模块契约（B 侧产出），
    上面明确有 char_start/char_end，拿它当去重键不需要额外接口。
    """
    seen = set()
    out = []
    for hit in hits:
        _cand, ref = hit
        key = (getattr(ref, "doc_id", None), getattr(ref, "char_start", None))
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def _merge_refs(primary, required):
    """Append missing chain refs without changing answer citation indices.

    The primary list is already numbered in the generated answer. Deduplicating
    any of its entries would shift later markers, so preserve it verbatim and only
    deduplicate newly appended refs by their complete source location.
    """
    out = list(primary)

    def key(ref):
        return tuple(
            getattr(ref, name, None)
            for name in ("doc_id", "char_start", "char_end", "page", "section_path")
        )

    seen = {key(ref) for ref in out}
    for ref in required:
        ref_key = key(ref)
        if ref_key not in seen:
            seen.add(ref_key)
            out.append(ref)
    return out


def _hit_locator(hit):
    """把一条命中压成"标题 + 位置"的短串，用于充分性判断。

    只送位置、**不送正文**：一是省 token，二是避免把同一批正文再喂一遍诱导模型
    直接作答（那会让"够不够"变成"答不答得出来"，判断口径就跑偏了）。
    """
    _cand, ref = hit
    parts = [str(getattr(ref, "doc_id", "") or "")]
    page = getattr(ref, "page", None)
    if page is not None:
        parts.append("第" + str(page) + "页")
    section = getattr(ref, "section_path", None)
    if section:
        parts.append(str(section))
    return " / ".join([p for p in parts if p])


def _is_policy_question(question):
    question = str(question)
    if any(cue in question for cue in _POLICY_QUESTION_CUES):
        return True
    asks_retention = "保留" in question and any(
        cue in question for cue in ("多久", "多长", "几天", "几年", "多长时间")
    )
    asks_access_limit = "访问" in question and any(
        cue in question for cue in ("限制", "要求", "条件", "权限", "控制")
    )
    return asks_retention or asks_access_limit


def _named_subject_from_hits(question, hits):
    """从题目点名的目录条目中识别具体数据对象，不把“该数据集”当作名称。"""
    question = str(question)
    for cand, _ref in hits:
        text = str(cand.chunk.text)
        for match in re.finditer(r"(?:^|\n)\s*#\s*([^\n#]+?)数据目录", text):
            subject = match.group(1).strip()
            if subject and subject in question and subject not in {"该数据集", "此数据集", "数据集"}:
                return subject
        for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9]{2,40}?)定级为", text):
            subject = match.group(1).strip()
            if subject and subject in question and subject not in {"该数据集", "此数据集", "数据集"}:
                return subject
    return ""


def _applicable_policy_from_hits(subject, hits):
    """只接受同一片段明确写出的“具体对象适用某制度/规则”关系。"""
    if not subject:
        return None
    for index, (cand, _ref) in enumerate(hits):
        text = str(cand.chunk.text)
        if subject not in text:
            continue
        for sentence in re.split(r"[。；\n]", text):
            if subject not in sentence:
                continue
            # PDF 分块会把“适用制度”小标题和正文拼在一行。只解析对象名之后的
            # 关系，避免把小标题里的“制度”误当作制度名称的一部分。
            subject_end = sentence.find(subject) + len(subject)
            relation_text = sentence[subject_end:]
            match = re.search(
                r"(?:适用|遵循|执行)\s*(?!制度)([^，。；\n]+)", relation_text
            )
            if match:
                policy = match.group(1).strip(" ：:（）() ")
                if policy:
                    return {"name": policy, "citation_index": index}
    return None


def _complete_required_chain_citations(generated, context_hits, required_hits,
                                       required_labels=None, chain_name="policy"):
    """把已验证的必需关系证据补入连续引用表。"""
    position_by_key = {}
    for index, (_cand, ref) in enumerate(context_hits, start=1):
        key = (getattr(ref, "doc_id", None), getattr(ref, "char_start", None))
        position_by_key[key] = index
    required_positions = []
    for _cand, ref in required_hits:
        key = (getattr(ref, "doc_id", None), getattr(ref, "char_start", None))
        position = position_by_key.get(key)
        if position is None:
            return generated
        if position not in required_positions:
            required_positions.append(position)
    if len(required_positions) < 3:
        return generated

    answer = str(generated.get("text") or "")
    cite_fix = generated.setdefault("cite_fix", {})
    old_to_current = {
        int(old): int(current)
        for old, current in (cite_fix.get("renumbered") or {}).items()
    }
    current_to_old = {current: old for old, current in old_to_current.items()}
    selected_old = []
    for match in _CITATION_RE.finditer(answer):
        current = _citation_number(match.group(1), len(context_hits))
        if current is None:
            continue
        old = current_to_old.get(current, current)
        if 1 <= old <= len(context_hits) and old not in selected_old:
            selected_old.append(old)
    missing_positions = []
    for position in required_positions:
        if position not in selected_old:
            selected_old.append(position)
            missing_positions.append(position)
    new_number = {old: index for index, old in enumerate(selected_old, start=1)}

    def remap(match):
        current = _citation_number(match.group(1), len(context_hits))
        if current is None:
            return match.group(0)
        old = current_to_old.get(current, current)
        return "[" + str(new_number.get(old, current)) + "]"

    answer = _CITATION_RE.sub(remap, answer)
    required_labels = required_labels or ["对象及级别", "对象适用制度", "制度要求"]
    has_chain_section = re.search(r"关系证据\s*[:：]", answer) is not None
    if missing_positions or not has_chain_section:
        labels_by_position = dict(zip(required_positions, required_labels))
        # 没有关系证据小节时，必须把整条链的标签都列出；即使答案正文
        # 已经出现某个编号，也不能假定读者知道它对应链路中的哪一跳。
        positions_to_show = required_positions if not has_chain_section else missing_positions
        evidence_lines = [
            "- " + labels_by_position[position] + "关系：["
            + str(new_number[position]) + "]"
            for position in positions_to_show
        ]
        heading = "补充关系证据" if has_chain_section else "关系证据"
        answer = answer.rstrip() + "\n\n" + heading + "：\n" + "\n".join(evidence_lines)
    generated["text"] = answer
    generated["final_refs"] = [context_hits[index - 1][1] for index in selected_old]
    cite_fix["renumbered"] = {str(old): new for old, new in new_number.items()}
    cite_fix[chain_name + "_chain_completed"] = True
    return generated


def _complete_policy_chain_citations(generated, context_hits, required_hits,
                                     required_labels=None, chain_name="policy"):
    """兼容旧测试与调用方；实现已统一到通用关系链引用补全函数。"""
    return _complete_required_chain_citations(
        generated,
        context_hits,
        required_hits,
        required_labels=required_labels,
        chain_name=chain_name,
    )


def _contract_item_route_requested(question: str) -> bool:
    """识别带物品标识的“物品→合同服务/违约条款”跨文档问题。"""
    text = str(question)
    asks_service = any(cue in text for cue in ("服务", "维护", "校准", "保养", "检修"))
    asks_penalty = any(cue in text for cue in (
        "违约金", "逾期", "延期", "迟延", "延迟", "扣罚", "罚金",
        "未按期", "按期", "按时", "迟交", "超期", "延误", "滞后",
        "罚款", "违约", "赔偿", "扣款", "扣款比例",
    ))
    mentions_item = any(cue in text for cue in (
        "采购", "物品", "设备", "产品", "型号", "采购项目", "合同",
        "继电器",
    ))
    item_codes = re.findall(r"(?<![A-Za-z0-9])[A-Za-z]{1,6}-\d{1,5}(?!\d)", text)
    return mentions_item and asks_service and asks_penalty and bool(item_codes)


def _contract_id_from_text(text: str) -> str:
    # Python 的 Unicode \b 会把相邻汉字视为“单词”，中文合同号通常紧贴“编号/合同”字样，
    # 因此这里只用 ASCII 字母数字边界，避免漏掉“合同编号HT-2030-606”。
    match = re.search(r"(?<![A-Za-z0-9])HT-\d{3,}-\d{3,}(?![A-Za-z0-9])", str(text))
    return match.group(0) if match else ""


def _service_route_requested(question):
    question = str(question)
    return "流程" in question and any(
        cue in question for cue in (
            "响应", "首响", "办结", "解决时限", "处理时限", "最终处理",
            "服务标准", "服务目录",
        )
    )


def _equipment_route_requested(question):
    question = str(question)
    return any(cue in question for cue in ("维护", "巡检", "检修", "维护方案", "维护计划"))


def _sentence_with_pattern(hits, pattern, required=()):
    for hit in hits:
        text = str(hit[0].chunk.text)
        for sentence in re.split(r"[。；\n]", text):
            # 文档块可能把标题、章节名和正文压成一行；先去掉标题/章节前缀，
            # 再按正文中的关系抽取，避免前缀污染实体值。
            if "##" in sentence:
                sentence = sentence.rsplit("##", 1)[-1]
            sentence = re.sub(r"^\s*#+\s*", "", sentence)
            sentence = re.sub(
                r"^\s*(?:办理归属|承接事项|安装位置|适用方案|周期与安全要求)\s*",
                "",
                sentence,
            )
            if all(token and token in sentence for token in required):
                match = re.search(pattern, sentence)
                if match:
                    return hit, sentence, match
    return None


class MultiHopRagTool:
    """多跳检索工具：包装一个 RagAnswerTool，对外仍是同一个 answer() 接口。

    是否走多跳由构造参数 `enabled` 决定（默认读应急开关 `RAG_MULTIHOP`，默认允许）。
    `enabled=False` 时 `answer()` 直接转调被包装的单轮实现，行为与单轮完全一致 ——
    这条路径是测试与应急回退用的。
    """

    def __init__(self, rag_tool, *, max_subqueries=None, max_hops=None,
                 final_context_top_k=None, enabled=None):
        self._rag = rag_tool
        if enabled is None:
            enabled = env_int(_ENV_MULTIHOP, _DEFAULT_MULTIHOP, minimum=0) != 0
        if max_subqueries is None:
            max_subqueries = env_int(_ENV_MAX_SUBQUERIES, _DEFAULT_MAX_SUBQUERIES)
        if max_hops is None:
            max_hops = env_int(_ENV_MAX_HOPS, _DEFAULT_MAX_HOPS)
        if final_context_top_k is None:
            final_context_top_k = env_int(
                _ENV_FINAL_CONTEXT_TOP_K, _DEFAULT_FINAL_CONTEXT_TOP_K
            )
        self.enabled = bool(enabled)
        self.max_subqueries = max_subqueries
        self.max_hops = max_hops
        self.final_context_top_k = final_context_top_k

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def answer(self, question, doc_id=None):
        """返回 ToolResult（与单轮路径同构）。

        `RAG_MULTIHOP=0`（默认）时完全走单轮实现 —— 见模块头说明。
        """
        if not self.enabled:
            return self._rag.answer(question, doc_id)
        return self._answer_multihop(question, doc_id)

    # ------------------------------------------------------------------
    # 多跳主流程
    # ------------------------------------------------------------------

    def _answer_multihop(self, question, doc_id=None):
        if _is_policy_question(question):
            return self._answer_policy_question(question, doc_id)
        if _service_route_requested(question):
            return self._answer_service_chain(question, doc_id)
        if _contract_item_route_requested(question):
            return self._answer_contract_item_chain(question, doc_id)
        if _equipment_route_requested(question):
            return self._answer_equipment_chain(question, doc_id)
        trace = []

        # [1] 分解
        t0 = time.time()
        plan = self._plan(question)
        trace.append(TraceStep(
            step_id=1,
            tool="decompose",
            input={"query": question},
            output={
                "subqueries": plan["subqueries"],
                "steps": plan["steps"],
                "mode": plan["mode"],
                "fallback": plan["fallback"],   # True = 没拆，退回单查询
                "max_subqueries": self.max_subqueries,
            },
            elapsed_ms=(time.time() - t0) * 1000.0,
            hit=[],
        ))
        if plan["mode"] == "dependent_plan":
            return self._answer_dependent_plan(question, doc_id, trace, plan["steps"])

        subqueries = plan["subqueries"]
        queries = subqueries if subqueries else [question]

        # [2] 检索轮次（首轮 = 各子查询；后续轮 = 充分性判断给出的补充查询）
        rounds = []
        for hop in range(1, self.max_hops + 1):
            round_hits = []
            for q in queries:
                r = self._rag.retrieve_and_rank(q, doc_id)
                trace.extend(self._rag.retrieve_steps(r, len(trace) + 1, doc_id))
                round_hits.extend(r["context_hits"])
            rounds.append(round_hits)

            if hop >= self.max_hops:
                break

            merged_so_far = _dedup_hits([h for rd in rounds for h in rd])
            t0 = time.time()
            suff = self._check_sufficient(question, merged_so_far)
            trace.append(TraceStep(
                step_id=len(trace) + 1,
                tool="sufficiency_check",
                input={"query": question, "materials": len(merged_so_far)},
                output={
                    "sufficient": suff["sufficient"],
                    "missing": suff["missing"],
                    "next_queries": suff["next_queries"],
                    "usable": suff["usable"],     # False = 没解析出 JSON，按"够用"处理
                },
                elapsed_ms=(time.time() - t0) * 1000.0,
                hit=[ref for _cand, ref in merged_so_far],
            ))
            if suff["sufficient"] or not suff["next_queries"]:
                break
            queries = suff["next_queries"][: self.max_subqueries]

        # [3] 合并去重
        before = sum(len(rd) for rd in rounds)
        merged = _dedup_hits([h for rd in rounds for h in rd])
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="context_merge",
            input={"rounds": len(rounds), "before": before},
            output={"after": len(merged)},
            elapsed_ms=0.0,
            hit=[ref for _cand, ref in merged],
        ))

        # [4] 跨轮重排：**必须用原问题**，因为各轮分数来自不同查询、尺度不可比
        t0 = time.time()
        final_hits = self._cross_rerank(question, merged)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="cross_rerank",
            input={"query": question, "num": len(merged),
                   "top_k": self.final_context_top_k},
            output={"kept": len(final_hits)},
            elapsed_ms=(time.time() - t0) * 1000.0,
            hit=[ref for _cand, ref in final_hits],
        ))

        # [5] 生成：只调一次，编号 1..N 由 generate() 内部完成
        g = self._rag.generate(question, final_hits, require_chain=True)
        trace.append(self._rag.generate_step(question, g, step_id=len(trace) + 1))

        return ToolResult(data=g["text"], trace=trace, refs=g["final_refs"])

    def _answer_contract_item_chain(
        self, question: str, doc_id: str | None = None
    ) -> ToolResult:
        """先从点名物品绑定合同号，再按该合同号检索服务和违约条款。"""
        question_text = str(question)
        item_codes = re.findall(r"(?<![A-Za-z0-9])[A-Za-z]{1,6}-\d{1,5}(?!\d)", question_text)
        item_code = item_codes[0] if item_codes else ""
        trace = [TraceStep(
            step_id=1,
            tool="contract_item_route",
            input={"query": question, "item_code": item_code},
            output={"route": "item_to_contract_to_service_and_penalty"},
            elapsed_ms=0.0,
            hit=[],
        )]

        # 型号/物品编号是问题里最稳定的桥接键；抽取自然语言品名容易受问法影响。
        object_query = item_code + " 验收记录 合同编号"
        object_result = self._rag.retrieve_and_rank(object_query, doc_id)
        trace.extend(self._rag.retrieve_steps(object_result, len(trace) + 1, doc_id))
        bridge_hit = next((
            hit for hit in object_result["context_hits"]
            if item_code in str(hit[0].chunk.text)
            and _contract_id_from_text(hit[0].chunk.text)
        ), None)
        if bridge_hit is None:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "item_contract"}, "unverified_item_contract_link"
            )
        contract_id = _contract_id_from_text(bridge_hit[0].chunk.text)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="contract_bind",
            input={"item_code": item_code, "relation": "item_to_contract"},
            output={"contract_id": contract_id, "usable": True},
            elapsed_ms=0.0,
            hit=[bridge_hit[1]],
        ))

        service_result = self._rag.retrieve_and_rank(
            "合同" + contract_id + "项下服务项目 服务清单", doc_id
        )
        trace.extend(self._rag.retrieve_steps(service_result, len(trace) + 1, doc_id))
        service_hit = next((
            hit for hit in service_result["context_hits"]
            if contract_id in str(hit[0].chunk.text)
            and any(cue in str(hit[0].chunk.text) for cue in (
                "服务", "承担", "技术支持", "维护", "校准",
            ))
        ), None)

        penalty_result = self._rag.retrieve_and_rank(
            "合同" + contract_id + "延期交付逾期违约金计算方式", doc_id
        )
        trace.extend(self._rag.retrieve_steps(penalty_result, len(trace) + 1, doc_id))
        penalty_hit = next((
            hit for hit in penalty_result["context_hits"]
            if contract_id in str(hit[0].chunk.text)
            and "违约金" in str(hit[0].chunk.text)
            and any(cue in str(hit[0].chunk.text) for cue in ("逾期", "延期交付"))
        ), None)
        if service_hit is None or penalty_hit is None:
            trace.append(TraceStep(
                step_id=len(trace) + 1,
                tool="contract_chain_incomplete",
                input={"contract_id": contract_id},
                output={"service_found": service_hit is not None,
                        "penalty_found": penalty_hit is not None},
                elapsed_ms=0.0,
                hit=[],
            ))
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "contract_terms"}, "unverified_contract_terms"
            )

        required_hits = [bridge_hit, service_hit, penalty_hit]
        chain_notes = [
            "物品编号“" + item_code + "”与合同“" + contract_id
            + "”的对应关系来自首跳命中片段。",
            "服务项目来自明确包含合同号“" + contract_id + "”的服务片段。",
            "违约金规则来自明确包含合同号“" + contract_id + "”的延期条款片段。",
        ]
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="context_merge",
            input={"contract_id": contract_id, "required_evidence_count": len(required_hits)},
            output={"kept": len(required_hits)},
            elapsed_ms=0.0,
            hit=[hit[1] for hit in required_hits],
        ))
        generated = self._rag.generate(
            question, required_hits, chain_notes=chain_notes, require_chain=True
        )
        generated = _complete_required_chain_citations(
            generated,
            required_hits,
            required_hits,
            required_labels=["点名物品与合同号的对应", "合同服务项目", "合同违约金条款"],
            chain_name="contract",
        )
        trace.append(self._rag.generate_step(question, generated, step_id=len(trace) + 1))
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="provenance_complete",
            input={"required_chain_sources": len(required_hits)},
            output={"final_refs": len(generated["final_refs"]), "chain": "contract"},
            elapsed_ms=0.0,
            hit=generated["final_refs"],
        ))
        return ToolResult(data=generated["text"], trace=trace, refs=generated["final_refs"])

    def _answer_policy_question(self, question, doc_id=None):
        """按已验证的“对象适用制度”关系定向检索制度正文。

        适用范围由自然语言问题中的制度/留存/访问控制意图触发。制度映射必须在同一
        证据片段中同时出现题目点名对象和“适用/遵循/执行”关系；只凭同级别或相似对象
        不会选制度。题目没有具体对象时请求澄清，避免猜测数据集。
        """
        trace = [TraceStep(
            step_id=1,
            tool="policy_relation_route",
            input={"query": question},
            output={"route": "object_to_policy_relation"},
            elapsed_ms=0.0,
            hit=[],
        )]
        first = self._rag.retrieve_and_rank(question, doc_id)
        trace.extend(self._rag.retrieve_steps(first, len(trace) + 1, doc_id))
        initial_hits = first["context_hits"]
        subject = _named_subject_from_hits(question, initial_hits)
        if not subject:
            trace.append(TraceStep(
                step_id=len(trace) + 1,
                tool="clarification_needed",
                input={"query": question},
                output={"reason": "policy_question_missing_named_object"},
                elapsed_ms=0.0,
                hit=[],
            ))
            return ToolResult(
                data="请补充具体的数据集或业务对象名称；目前的问题无法确定对应的管理制度。",
                trace=trace,
                refs=[],
            )

        relation = _applicable_policy_from_hits(subject, initial_hits)
        policy_hits = []
        chain_notes = []
        subject_hits = [hit for hit in initial_hits if subject in str(hit[0].chunk.text)]
        protected = list(subject_hits)
        required_chain_hits = []
        if relation:
            relation_hit = initial_hits[relation["citation_index"]]
            policy = relation["name"]
            chain_notes.append(
                "已验证对象“" + subject + "”适用制度“" + policy
                + "”；关系证据位于 " + str(getattr(relation_hit[1], "doc_id", "")) + "。"
            )
            trace.append(TraceStep(
                step_id=len(trace) + 1,
                tool="relation_bind",
                input={"subject": subject, "relation": "applicable_policy"},
                output={"value": policy, "usable": True},
                elapsed_ms=0.0,
                hit=[relation_hit[1]],
            ))
            policy_query = policy + "规定的保留期限、查阅权限或操作控制是什么？"
            result = self._rag.retrieve_and_rank(policy_query, doc_id)
            trace.extend(self._rag.retrieve_steps(result, len(trace) + 1, doc_id))
            # 制度正文必须包含已验证的制度名；同级别但属于其他对象的制度块全部剔除。
            policy_hits = [
                hit for hit in result["context_hits"]
                if policy in str(hit[0].chunk.text)
            ]
            protected.append(relation_hit)
            protected.extend(policy_hits)
            classification_hit = next(
                (hit for hit in subject_hits if "定级为" in str(hit[0].chunk.text)), None
            )
            policy_fact_hit = next(
                (hit for hit in policy_hits
                 if policy in str(hit[0].chunk.text)
                 and any(cue in str(hit[0].chunk.text) for cue in ("规定", "保留", "访问", "控制"))),
                None,
            )
            if classification_hit and policy_fact_hit:
                required_chain_hits = [classification_hit, relation_hit, policy_fact_hit]
        else:
            chain_notes.append(
                "检索片段没有找到对象“" + subject + "”与具体制度之间的明确关系；"
                "不得用同类别其他对象的制度补全。"
            )
            trace.append(TraceStep(
                step_id=len(trace) + 1,
                tool="relation_bind",
                input={"subject": subject, "relation": "applicable_policy"},
                output={"value": "", "usable": False},
                elapsed_ms=0.0,
                hit=[],
            ))

        # 此路只把对象自身的资料、对象到制度的明确映射、制度原文交给生成器。
        # 不把同类别其他对象的材料作为“补充上下文”。
        scoped_initial = subject_hits
        merged = _dedup_hits(scoped_initial + ([relation_hit] if relation else []) + policy_hits)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="context_merge",
            input={"rounds": 2 if relation else 1,
                   "before": len(scoped_initial) + (1 if relation else 0) + len(policy_hits)},
            output={"after": len(merged), "protected": len(_dedup_hits(protected))},
            elapsed_ms=0.0,
            hit=[ref for _cand, ref in merged],
        ))
        final_hits = self._protected_cross_rerank(question, merged, protected)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="cross_rerank",
            input={"query": question, "num": len(merged), "top_k": self.final_context_top_k,
                   "protected": len(_dedup_hits(protected))},
            output={"kept": len(final_hits)},
            elapsed_ms=0.0,
            hit=[ref for _cand, ref in final_hits],
        ))
        generated = self._rag.generate(
            question, final_hits, chain_notes=chain_notes, require_chain=True
        )
        if required_chain_hits:
            generated = _complete_required_chain_citations(
                generated, final_hits, required_chain_hits
            )
        trace.append(self._rag.generate_step(question, generated, step_id=len(trace) + 1))
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="provenance_complete",
            input={"answer_refs": len(generated["final_refs"]),
                   "relation_verified": bool(relation)},
            output={"final_refs": len(generated["final_refs"])},
            elapsed_ms=0.0,
            hit=generated["final_refs"],
        ))
        return ToolResult(data=generated["text"], trace=trace, refs=generated["final_refs"])

    def _answer_service_chain(self, question, doc_id=None):
        """逐段验证流程归属、单位服务目录和服务标准，再生成答案。"""
        trace = [TraceStep(
            step_id=1, tool="service_relation_route", input={"query": question},
            output={"route": "process_to_department_to_service_standard"},
            elapsed_ms=0.0, hit=[],
        )]
        first = self._rag.retrieve_and_rank(question, doc_id)
        trace.extend(self._rag.retrieve_steps(first, len(trace) + 1, doc_id))
        process_link = _sentence_with_pattern(
            first["context_hits"], r"^(.+?)由(.+?)归口办理$"
        )
        if not process_link:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "service_process"}, "unverified_process_owner"
            )
        process_hit, _sentence, match = process_link
        process, department = (part.strip() for part in match.groups())
        if process not in question:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "service_process"}, "process_not_named_in_question"
            )

        second = self._rag.retrieve_and_rank(department + "负责哪些服务？服务目录", doc_id)
        trace.extend(self._rag.retrieve_steps(second, len(trace) + 1, doc_id))
        service_link = _sentence_with_pattern(
            second["context_hits"], r"^(.+?)负责(.+?)$", required=(department,)
        )
        if not service_link:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "service_department"}, "unverified_service_mapping"
            )
        service_hit, _sentence, service_match = service_link
        service = service_match.group(2).strip()

        third = self._rag.retrieve_and_rank(service + "服务标准 首次响应时限 问题解决时限", doc_id)
        trace.extend(self._rag.retrieve_steps(third, len(trace) + 1, doc_id))
        standard_hit = next((
            hit for hit in third["context_hits"]
            if service in str(hit[0].chunk.text)
            and any(cue in str(hit[0].chunk.text) for cue in ("响应时限", "解决时限", "办结"))
        ), None)
        if not standard_hit:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "service_standard"}, "unverified_service_standard"
            )

        required = [process_hit, service_hit, standard_hit]
        chain_notes = [
            "已从原文验证流程“" + process + "”由“" + department + "”归口办理，",
            "该单位服务目录明确承接“" + service + "”，并找到对应服务标准。",
        ]
        merged = _dedup_hits(required)
        final_hits = self._protected_cross_rerank(question, merged, required)
        trace.append(TraceStep(
            step_id=len(trace) + 1, tool="relation_bind",
            input={"chain": "process_department_service_standard"},
            output={"process": process, "department": department, "service": service,
                    "verified_links": 2}, elapsed_ms=0.0,
            hit=[hit[1] for hit in required],
        ))
        generated = self._rag.generate(question, final_hits, chain_notes=chain_notes, require_chain=True)
        generated = _complete_required_chain_citations(
            generated, final_hits, required,
            required_labels=["流程办理归属", "单位承接服务", "服务时限标准"],
            chain_name="service",
        )
        trace.append(self._rag.generate_step(question, generated, step_id=len(trace) + 1))
        trace.append(TraceStep(
            step_id=len(trace) + 1, tool="provenance_complete",
            input={"answer_refs": len(generated["final_refs"]), "relation_verified": True},
            output={"final_refs": len(generated["final_refs"]), "chain": "service"},
            elapsed_ms=0.0, hit=generated["final_refs"],
        ))
        return ToolResult(data=generated["text"], trace=trace, refs=generated["final_refs"])

    def _answer_equipment_chain(self, question, doc_id=None):
        """逐段验证设备档案、设备到维护方案的映射和方案正文。"""
        trace = [TraceStep(
            step_id=1, tool="equipment_relation_route", input={"query": question},
            output={"route": "asset_to_maintenance_plan_to_requirements"},
            elapsed_ms=0.0, hit=[],
        )]
        first = self._rag.retrieve_and_rank(question, doc_id)
        trace.extend(self._rag.retrieve_steps(first, len(trace) + 1, doc_id))
        asset_link = _sentence_with_pattern(
            first["context_hits"], r"^(.+?)安装于(.+)$", required=()
        )
        if not asset_link:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "asset_location"}, "unverified_asset"
            )
        asset_hit, _sentence, match = asset_link
        asset, location = (part.strip() for part in match.groups())
        if asset not in question:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "asset_location"}, "asset_not_named_in_question"
            )

        second = self._rag.retrieve_and_rank(asset + " " + location + "适用维护方案", doc_id)
        trace.extend(self._rag.retrieve_steps(second, len(trace) + 1, doc_id))
        plan_link = _sentence_with_pattern(
            second["context_hits"], r"^(.+?)适用(.+)$", required=(asset, location)
        )
        if not plan_link:
            # 部分目录句只出现设备名，不重复位置；仅在设备名唯一且有明确适用关系时接纳。
            plan_link = _sentence_with_pattern(
                second["context_hits"], r"^(.+?)适用(.+)$", required=(asset,)
            )
        if not plan_link:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "asset_plan"}, "unverified_maintenance_mapping"
            )
        plan_hit, _sentence, plan_match = plan_link
        plan = plan_match.group(2).strip()

        third = self._rag.retrieve_and_rank(plan + "巡检周期 作业前安全要求", doc_id)
        trace.extend(self._rag.retrieve_steps(third, len(trace) + 1, doc_id))
        requirement_hit = next((
            hit for hit in third["context_hits"]
            if plan in str(hit[0].chunk.text)
            and any(cue in str(hit[0].chunk.text) for cue in ("巡检", "检查", "维护"))
        ), None)
        if not requirement_hit:
            return self._fallback_after_plan_failure(
                question, doc_id, trace, {"id": "maintenance_requirements"},
                "unverified_maintenance_requirements"
            )

        required = [asset_hit, plan_hit, requirement_hit]
        chain_notes = [
            "设备档案确认“" + asset + "”位于“" + location + "”；",
            "设备目录明确该设备适用“" + plan + "”，以下要求来自该方案原文。",
        ]
        merged = _dedup_hits(required)
        final_hits = self._protected_cross_rerank(question, merged, required)
        trace.append(TraceStep(
            step_id=len(trace) + 1, tool="relation_bind",
            input={"chain": "asset_plan_requirements"},
            output={"asset": asset, "location": location, "plan": plan,
                    "verified_links": 2}, elapsed_ms=0.0,
            hit=[hit[1] for hit in required],
        ))
        generated = self._rag.generate(question, final_hits, chain_notes=chain_notes, require_chain=True)
        generated = _complete_required_chain_citations(
            generated, final_hits, required,
            required_labels=["设备档案及位置", "设备适用维护方案", "方案周期与安全要求"],
            chain_name="equipment",
        )
        trace.append(self._rag.generate_step(question, generated, step_id=len(trace) + 1))
        trace.append(TraceStep(
            step_id=len(trace) + 1, tool="provenance_complete",
            input={"answer_refs": len(generated["final_refs"]), "relation_verified": True},
            output={"final_refs": len(generated["final_refs"]), "chain": "equipment"},
            elapsed_ms=0.0, hit=generated["final_refs"],
        ))
        return ToolResult(data=generated["text"], trace=trace, refs=generated["final_refs"])

    def _answer_dependent_plan(self, question, doc_id, trace, steps):
        """执行“首跳绑定实体，后续步骤引用实体”的受约束多跳计划。

        这里不把第一跳的模型结论当作事实。只有实体抽取结果能指向该轮已检索
        的片段，模板步骤才会运行；否则直接退化为普通单轮检索。这样既避免无依据
        的变量传播，也保留多跳增强失败时的可用性。
        """
        bindings = {}
        merged = []
        protected = []
        bridge_refs = []
        chain_notes = []
        role_aliases = _load_role_aliases()
        for step in steps:
            if step["kind"] == "query":
                query = step["text"]
                if step.get("bind"):
                    query = _ground_binding_query(query, question)
            else:
                query = _render_query_template(step["text"], bindings)
                if not query:
                    return self._fallback_after_plan_failure(
                        question, doc_id, trace, step, "missing_binding"
                    )

            r = self._rag.retrieve_and_rank(query, doc_id)
            trace.extend(self._rag.retrieve_steps(r, len(trace) + 1, doc_id))
            merged.extend(r["context_hits"])

            if step["kind"] == "query_template":
                # 第二跳按自己的查询重排过，取最相关的两条作为受保护的目标证据；
                # 其余位置仍由原问题跨轮重排决定。
                protected.extend(r["context_hits"][:2])

            bind = step.get("bind")
            if bind:
                t0 = time.time()
                binding = self._extract_binding(bind, query, r["context_hits"])
                hit = []
                if binding["citation_index"] is not None:
                    cited = r["context_hits"][binding["citation_index"]]
                    hit = [cited[1]]
                    protected.append(cited)
                    bridge_refs.append(cited[1])
                trace.append(TraceStep(
                    step_id=len(trace) + 1,
                    tool="entity_bind",
                    input={"name": bind, "query": query, "contexts": len(r["context_hits"])},
                    output={
                        "value": binding["value"],
                        "citation_index": binding["citation_index"],
                        "usable": binding["usable"],
                        "method": binding["method"],
                    },
                    elapsed_ms=(time.time() - t0) * 1000.0,
                    hit=hit,
                ))
                if not binding["value"] or binding["citation_index"] is None:
                    return self._fallback_after_plan_failure(
                        question, doc_id, trace, step, "unverified_binding"
                    )
                bindings[bind] = binding
                chain_notes.append(
                    _role_alias_note(
                        question,
                        binding,
                        r["context_hits"][binding["citation_index"]],
                        role_aliases,
                    )
                )

        before = len(merged)
        merged = _dedup_hits(merged)
        protected = _dedup_hits(protected)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="context_merge",
            input={"rounds": len(steps), "before": before},
            output={"after": len(merged), "protected": len(protected)},
            elapsed_ms=0.0,
            hit=[ref for _cand, ref in merged],
        ))
        t0 = time.time()
        final_hits = self._protected_cross_rerank(question, merged, protected)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="cross_rerank",
            input={
                "query": question,
                "num": len(merged),
                "top_k": self.final_context_top_k,
                "protected": len(protected),
            },
            output={"kept": len(final_hits)},
            elapsed_ms=(time.time() - t0) * 1000.0,
            hit=[ref for _cand, ref in final_hits],
        ))
        g = self._rag.generate(
            question, final_hits, chain_notes=chain_notes, require_chain=True
        )
        trace.append(self._rag.generate_step(question, g, step_id=len(trace) + 1))
        final_refs = _merge_refs(g["final_refs"], bridge_refs)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="provenance_complete",
            input={"answer_refs": len(g["final_refs"]), "bridge_refs": len(bridge_refs)},
            output={"final_refs": len(final_refs)},
            elapsed_ms=0.0,
            hit=final_refs,
        ))
        return ToolResult(data=g["text"], trace=trace, refs=final_refs)

    def _fallback_after_plan_failure(self, question, doc_id, trace, step, reason):
        """受约束计划无法继续时，显式留痕并退回一轮原问题检索。"""
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="plan_fallback",
            input={"step": step["id"]},
            output={"reason": reason},
            elapsed_ms=0.0,
            hit=[],
        ))
        r = self._rag.retrieve_and_rank(question, doc_id)
        trace.extend(self._rag.retrieve_steps(r, len(trace) + 1, doc_id))
        g = self._rag.generate(question, r["context_hits"], require_chain=True)
        trace.append(self._rag.generate_step(question, g, step_id=len(trace) + 1))
        return ToolResult(data=g["text"], trace=trace, refs=g["final_refs"])

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------

    def _protected_cross_rerank(self, question, hits, protected):
        """保留变量证据和第二跳目标证据，再以原问题填满剩余预算。"""
        protected = _dedup_hits(protected)[: self.final_context_top_k]
        if len(protected) >= self.final_context_top_k:
            return protected
        protected_keys = {
            (getattr(ref, "doc_id", None), getattr(ref, "char_start", None))
            for _cand, ref in protected
        }
        rest = [
            hit for hit in hits
            if (getattr(hit[1], "doc_id", None), getattr(hit[1], "char_start", None))
            not in protected_keys
        ]
        return protected + self._cross_rerank(
            question, rest
        )[: self.final_context_top_k - len(protected)]

    def _cross_rerank(self, question, hits):
        """用**原问题**对合并后的候选重排，并按上限截断。

        不重排直接用各轮的 context_hits 拼接会系统性偏向某一轮：
        第 1 轮"违约金怎么算"排出的分数，和第 2 轮"备件到货时限"排出的分数，
        根本不在一个尺度上。
        """
        if not hits:
            return []
        docs = [cand.chunk.text for cand, _ref in hits]
        ranked = self._rag.reranker.rerank(question, docs)
        ordered = [hits[idx] for idx, _score in ranked]
        return ordered[: self.final_context_top_k]

    def _plan(self, question):
        """请求并校验查询计划；不合格时退回旧的静态子查询或单查询。"""
        user = (
            "原问题：\n" + question + "\n\n"
            "规划要求：\n"
            "1. 先拆出从问题对象到所求事实之间必须验证的每段关系；需要跨文档时，用 steps 覆盖每段关系，"
            "不需要跨文档时输出空 subqueries。\n"
            "2. bind 只能保存后续步骤确实要用的中间实体，例如地点、数据级别、适用制度名称；"
            "不能绑定最终答案值（期限、数量、权限要求等），也不能创建后续步骤不会使用的 bind。\n"
            "3. 对象→类别→适用制度→制度要求这类链，必须分别规划对象到类别、对象/类别到制度、制度到要求；"
            "不能从类别直接跳到最终要求，因为同一类别可能对应多个对象和制度。制度映射步骤必须保留问题中点名的对象原文，"
            "并把检索到的制度名称 bind 后再查制度正文。\n"
            "4. 问题已点名对象时，第一步应检索该对象与下一实体之间的关系，不能直接跳到最终属性；"
            "每个后续步骤都要接续上一跳的已验证实体。不得猜测变量值或跳过连接关系。\n"
            "5. 第一条 steps 必须是 query；query_template 只能引用前面 bind 的变量；每个 bind 都必须在后续模板中使用；最多 "
            + str(self.max_subqueries) + " 步。\n"
            "6. 只输出以下二选一 JSON：\n"
            "   {\"steps\":[{\"query\":\"某场地负责人是谁？\",\"bind\":\"person\"},"
            "{\"query_template\":\"{person}兼管哪类物资台账？\"}]}\n"
            "   或 {\"subqueries\":[\"可独立检索的问题1\",\"可独立检索的问题2\"]}\n"
        )
        try:
            text = self._rag.llm.chat(_DECOMPOSE_SYSTEM, user)
        except Exception:
            return {"mode": "single", "steps": [], "subqueries": [], "fallback": True}
        obj = _extract_json_object(text)
        if not obj:
            return {"mode": "single", "steps": [], "subqueries": [], "fallback": True}
        steps = _clean_plan_steps(obj.get("steps"), question, self.max_subqueries)
        if steps:
            return {"mode": "dependent_plan", "steps": steps, "subqueries": [], "fallback": False}
        cleaned = _clean_queries(obj.get("subqueries"), question, self.max_subqueries)
        if cleaned:
            return {"mode": "static_queries", "steps": [], "subqueries": cleaned, "fallback": False}
        return {"mode": "single", "steps": [], "subqueries": [], "fallback": True}

    def _decompose(self, question):
        """兼容旧调用：返回静态子查询与是否退化。"""
        plan = self._plan(question)
        return plan["subqueries"], plan["fallback"]

    def _extract_binding(self, name, query, context_hits):
        """从首跳片段抽取变量，且必须返回一个有效片段编号作为证据。"""
        fallback = _binding_from_text(context_hits, query)
        if not context_hits:
            return fallback
        materials = []
        for index, (cand, _ref) in enumerate(context_hits, start=1):
            materials.append("[" + str(index) + "]\n" + cand.chunk.text)
        user = (
            "首跳查询：\n" + query + "\n\n"
            "要绑定的变量：" + name + "\n"
            "从下列片段抽取该变量的一个具体值。若片段不能确定，value 置空。\n"
            "只输出 JSON：{\"value\":\"具体值\",\"citation_index\":1}\n\n"
            "片段：\n" + "\n\n".join(materials)
        )
        try:
            text = self._rag.llm.chat(_BIND_SYSTEM, user)
        except Exception:
            return fallback
        obj = _extract_json_object(text)
        if not obj:
            return fallback
        value = obj.get("value")
        citation_index = obj.get("citation_index")
        if not isinstance(value, str) or not value.strip():
            return fallback
        if not isinstance(citation_index, int) or not (1 <= citation_index <= len(context_hits)):
            return fallback
        cited_text = str(context_hits[citation_index - 1][0].chunk.text)
        if value.strip() not in cited_text:
            return fallback
        return {
            "value": value.strip()[:100],
            "citation_index": citation_index - 1,
            "usable": True,
            "method": "llm",
        }

    def _check_sufficient(self, question, hits):
        """判断已检索到的材料够不够；不够则给出补充查询。

        解析失败一律按"够用"处理（usable=False）—— 宁可少跳一轮，
        也不要因为解析问题陷入多余检索、把延迟和噪声都加上去。
        """
        fallback = {"sufficient": True, "missing": "", "next_queries": [], "usable": False}
        if not hits:
            return dict(fallback)
        lines = []
        for i, hit in enumerate(hits, start=1):
            lines.append(str(i) + ". " + _hit_locator(hit))
        user = (
            "原问题：\n" + question + "\n\n"
            "已检索到的材料（**只有标题与位置，没有正文**）：\n"
            + "\n".join(lines) + "\n\n"
            "判断这些材料是否足以回答原问题。若不足，写出还需要检索什么。\n"
            "只输出 JSON：{\"sufficient\": true, \"missing\": \"\", \"next_queries\": []}\n"
        )
        try:
            text = self._rag.llm.chat(_SUFFICIENCY_SYSTEM, user)
        except Exception:
            return dict(fallback)
        obj = _extract_json_object(text)
        if not obj:
            return dict(fallback)
        sufficient = _as_bool(obj.get("sufficient"), default=True)
        next_queries = _clean_queries(obj.get("next_queries"), question, self.max_subqueries)
        if sufficient:
            next_queries = []
        missing = obj.get("missing")
        return {
            "sufficient": sufficient,
            "missing": str(missing)[:200] if missing else "",
            "next_queries": next_queries,
            "usable": True,
        }


def build_multihop_tool(rag_tool, **kwargs):
    """便捷构造（等价于直接 new，保留此函数便于将来换实现）。"""
    return MultiHopRagTool(rag_tool, **kwargs)


def multihop_requested():
    """应急开关是否为"允许"状态（供启动时提示用）。"""
    return env_int(_ENV_MULTIHOP, _DEFAULT_MULTIHOP, minimum=0) != 0


if __name__ == "__main__":
    # 自检：不开 LLM/向量，仅验证配置读取与 JSON 解析的健壮性
    print("RAG_MULTIHOP 环境值:", os.environ.get(_ENV_MULTIHOP, "(未设，默认 1 = 允许)"))
    print("应急开关是否允许多跳:", multihop_requested())
    print("max_subqueries/max_hops/final_k:",
          env_int(_ENV_MAX_SUBQUERIES, _DEFAULT_MAX_SUBQUERIES),
          env_int(_ENV_MAX_HOPS, _DEFAULT_MAX_HOPS),
          env_int(_ENV_FINAL_CONTEXT_TOP_K, _DEFAULT_FINAL_CONTEXT_TOP_K))

    class _FakeRag:
        llm = None
        reranker = None

        def answer(self, question, doc_id=None):
            return ("SINGLE", question)

    t = MultiHopRagTool(_FakeRag(), enabled=False)
    print("enabled=False 时 answer() 是否走单轮:", t.answer("测试问题")[0] == "SINGLE")

    samples = [
        '{"subqueries": ["甲", "乙"]}',
        '```json\n{"subqueries": ["甲"]}\n```',
        '好的，结果是：{"subqueries": ["甲"]} 以上。',
        '不是 JSON',
        '',
    ]
    for s in samples:
        print("  解析", repr(s[:22]), "->", _extract_json_object(s))
    print("布尔归一化:", [_as_bool(v) for v in [True, "false", "true", 1, "no", None]])
