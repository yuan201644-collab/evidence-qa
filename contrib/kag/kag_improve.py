"""KAG 抽取层改进：接入实现（需要装 KAG）

本文件是 `custom_extractor.py`（团队真实接入 KAG 0.8.0 建图链路的那份实现）
的可公开发布版：保留三项改进的核心逻辑，去掉与具体业务语料绑定的提示词与
行业词表，使任何人在干净的 KAG 环境里装上就能跑。

纯判定逻辑（可离线验证、不依赖 KAG）在同目录 `kag_normalize.py`，
其自测见 `test_kag_normalize.py`。本文件只负责把那些逻辑挂到 KAG 的抽取器上。

三项改进，逐条对照 KAG 0.8.0 上游
`kag/builder/component/extractor/schema_free_extractor.py`：

R1 —— 三元组结构修复（同步路径）
    上游 `_invoke` 第 521 行写作
        triples = (self.triples_extraction(passage, filtered_entities),)
    多了一个尾逗号。`triples_extraction` 本来返回 `List[list]`，加尾逗号后
    返回值变成「外层长度为 1 的元组，唯一元素才是三元组列表」。

    这份数据一路传到 `assemble_sub_graph_with_triples`，那里第 344-346 行是
        for tri in triples:
            if tri is None or len(tri) != 3:
                continue
    循环只转一圈，`tri` 拿到的是整个三元组列表而不是一条三元组；
    `len(tri)` 是「三元组条数」，几乎不可能恰好等于 3，于是整批 `continue` ——
    **所有关系边被静默丢弃**，建出的图里只剩实体节点，没有边。
    只有某段文本恰好抽出 3 条三元组时，这 3 条会被误当成一条的主谓宾，
    产生一条语义错误的边 —— 这解释了为什么坏图里仍能看到零星边。

    上游异步路径 `_ainvoke`（第 530-561 行）用
        triples, std_entities = await asyncio.gather(...)
    拿到的是正常的 `List[list]`，**没有**这个缺陷。同一份配置，
    同步与异步两条建图路径产出的图质量完全不同。

    修复点：把数据交给 `assemble_sub_graph` 之前归一化为 `list[list]`。
    修的是数据布局，不触碰上游抽取算法。

R2 —— 实体名归一延后到连线时刻
    上游比较实体名用 `processing_phrases`（`kag/common/utils.py:196`）：
        re.sub("[^A-Za-z0-9\u4e00-\u9fa5 ]", " ", phrase.lower()).strip()
    它保留内部空格。而同一实体的写法在「实体列表」与「三元组端点」里
    常常只差空格或换行（PDF 排版把产品名、机构名折成两行是最常见来源）。
    严格比较判定不等，实体被当成新节点二次插入，本应接上的边只能落到兜底类型。

    修复点：连线前用 `grounding_text`（上游字符类规则 + 去空白）匹配，
    命中则把端点替换成实体列表里的规范写法；未命中则原样保留，与上游一致。

R3 —— 图谱质量可观测量
    建图后统计边类型分布，产出「语义关系边占比」与「兜底类型边占比」，
    写成 `graph_metrics.json`。它回答「R1/R2 生效了没有」，
    不回答「关系抽取得对不对」——后者只能人工抽样核对。
    团队在同一批语料上观测到该比例 0.57% → 33.41%（152/455），
    只能说明边被赋上了类型，不能说明类型正确。

用法
----
1. 安装上游：`pip install openspg-kag==0.8.0`；
2. 把本目录加入 `PYTHONPATH`，并在建图前显式 `import kag_improve`
   （注册是 import 的副作用，必须触发一次）；
3. 建图配置里把抽取器类型名写成 `askdata_schema_free_extractor`
   （见同目录 `builder.example.yaml`，与团队真实运行配置一致）；
4. 建图结束后遍历图边，交给 `kag_normalize.build_graph_metrics()` 生成指标。

许可
----
本文件以 Apache-2.0 发布。它继承上游 `SchemaFreeExtractor`，属衍生作品；
上游 KAG 亦为 Apache-2.0，二者兼容。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

# `ExtractorABC` 在 `kag.interface` 里，不在子包里 ——
# `kag/builder/component/extractor/__init__.py` 是空文件。
from kag.interface import ExtractorABC
from kag.builder.component.extractor.schema_free_extractor import SchemaFreeExtractor

from kag_normalize import grounding_text, unpack_triples


class AskDataSchemaFreeExtractor(SchemaFreeExtractor):
    """在 KAG `SchemaFreeExtractor` 上叠加 R1 / R2。

    只覆写同步路径 `_invoke`。异步路径 `_ainvoke` 在上游本就没有 R1 缺陷，
    覆写它反而会与上游版本脱节，故不动。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # 运行时统计，仅用于观测，不参与建图结果。
        self.stats: Counter = Counter()

    def _invoke(self, input: Any, **kwargs: Any) -> List[Any]:
        # 与上游 `_invoke` 逐句对齐，只改 triples 的取值方式与归一化。
        title = input.name
        passage = title + "\n" + input.content
        out: List[Any] = []

        entities = self.named_entity_recognition(passage)
        sub_graph, entities = self.assemble_sub_graph_with_spg_records(entities)

        filtered_entities = [
            {k: v for k, v in ent.items() if k in ["name", "category"]}
            for ent in entities
        ]

        # R1：上游此处是 (self.triples_extraction(...),) —— 去掉尾逗号再归一化。
        raw_triples = self.triples_extraction(passage, filtered_entities)
        triples = unpack_triples(raw_triples)

        std_entities = self.named_entity_standardization(passage, filtered_entities)
        self.append_official_name(entities, std_entities)

        # R2：连线前用宽松口径把三元组端点对齐到实体列表的规范写法。
        triples = self._reconcile_endpoints(triples, entities)

        self.assemble_sub_graph(sub_graph, input, entities, triples)
        out.append(sub_graph)
        return out

    def _reconcile_endpoints(
        self, triples: List[List[Any]], entities: List[Dict[str, Any]]
    ) -> List[List[Any]]:
        """把三元组主/宾端点替换成实体列表里的规范写法。

        只在 `grounding_text` 匹配成功时替换。替换后，下游
        `assemble_sub_graph_with_triples` 内部的严格比较即可命中实体，
        边不会退化为兜底类型。
        """
        canonical: Dict[str, str] = {}
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            name = ent.get("name")
            if not name:
                continue
            key = grounding_text(name)
            if key and key not in canonical:
                canonical[key] = str(name)

        reconciled: List[List[Any]] = []
        for tri in triples:
            if len(tri) != 3:
                reconciled.append(tri)
                continue
            subject, predicate, obj = tri[0], tri[1], tri[2]
            hit_s = canonical.get(grounding_text(subject))
            hit_o = canonical.get(grounding_text(obj))
            if hit_s is not None and hit_s != subject:
                self.stats["endpoint_reconciled"] += 1
            if hit_o is not None and hit_o != obj:
                self.stats["endpoint_reconciled"] += 1
            reconciled.append(
                [
                    hit_s if hit_s is not None else subject,
                    predicate,
                    hit_o if hit_o is not None else obj,
                ]
            )
        return reconciled


# KAG 通过注册表把配置里的 `type` 字符串绑定到实现类
# （`kag/common/registry/registrable.py:508 Registrable.register`）。
# 建图配置写 `type: askdata_schema_free_extractor` 即可选中本类。
ExtractorABC.register("askdata_schema_free_extractor")(AskDataSchemaFreeExtractor)
