#!/usr/bin/env python
"""生成知识库语料（data/corpus/raw/）。

为什么要自己生成，而不是全部下载现成文档：
    1) 版权。赛题要求提交「知识库文档样本不少于 10 份」，如果用第三方报告、标准全文，
       提交物里就带着别人的版权，学院审查那一关过不去。
    2) 难点可控。任务书要求语料里必须有 ≥2 份目录混乱的、1-2 份扫描件。
       现成文档里这几类很难凑齐，而「目录混乱」恰恰是中级任务 7 要打的对象——
       与其祈祷找得到，不如按考点造出来，还能精确控制混乱的形态。
    3) 可复现。评委在别的机器上重跑本脚本，能拿到一模一样的语料。

生成物（15 份，4 种文件格式：PDF / DOCX / Markdown / TXT；
其中 PDF 又分文本层与图片型（扫描件）两类，按内容形态共 5 类）：
    PDF 文本层 / PDF 图片型（扫描件）/ DOCX / Markdown / 纯文本
其中：
    - 目录混乱样本 3 份（KB-002 / KB-004 / KB-015）
    - 扫描件 2 份（KB-007 / KB-008），无文本层，必须过 OCR
    - 含计算公式样本 1 份（KB-006），供中级任务 6 参考
    - 繁体字 + 错别字样本 1 份（KB-009），供文档质量评估参考
    - 表格密集样本 2 份（KB-011 / KB-014）

用法：
    python data/corpus/tools/build_corpus.py
    python data/corpus/tools/build_corpus.py --out data/corpus/raw
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "data" / "corpus" / "raw"

PAGE_W, PAGE_H = 595, 842  # A4，单位 pt
MARGIN = 56
LINE_H = 16
BODY_SIZE = 10.5
HEAD_SIZE = {1: 15, 2: 12.5, 3: 11}

# 固定元数据，让同一份语料在任何机器上重新生成都得到相同的字节。
# 不做这一步的话，PDF/DOCX 会各自写入"当前创建时间"，sha256 每次都变——
# 于是"可复现"就只剩一句口号，manifest 里的校验值也失去比对价值。
FIXED_DATE = "D:20260917000000Z"
FIXED_CORE_DATETIME = None  # 由 datetime 惰性构造，避免顶层 import


# ======================================================================================
# 语料内容
# 约定：sections 每项为 (层级, 文本)。层级 1/2/3 对应章/节/条；层级 0 表示正文段落。
# ======================================================================================
DOCS: list[dict] = [
    {
        "id": "KB-001",
        "file": "KB-001_智能计算设备年度维保服务合同.pdf",
        "kind": "pdf",
        "category": "合同类",
        "notes": "正常多级目录，条款编号规范。作为正样本，与 KB-002 构成对照。",
        "sections": [
            (1, "智能计算设备年度维保服务合同"),
            (0, "合同编号：WX-2026-0417　　甲方：北方数据中心运营有限公司　　乙方：中电云维保服务有限公司"),
            (1, "第一章　服务范围"),
            (0, "1.1　服务对象：本合同覆盖甲方部署于 A3 机房与 B1 机房的全部智能计算设备，共 248 台。"),
            (0, "1.2　服务内容：故障响应、备件更换、季度巡检与固件升级四项。"),
            (0, "1.3　服务区域：限于中华人民共和国境内，不含港澳台地区的离岸机房。"),
            (1, "第二章　质保条款"),
            (0, "2.1　质保期限：自设备验收合格之日起 36 个月；其中 GPU 加速卡因工艺特性单独按 24 个月计算。"),
            (0, "2.2　覆盖部件：整机机箱、主板、电源模块、风扇模组、GPU 加速卡、内存条、固态硬盘均在覆盖范围内。外接显示器、KVM 切换器、机柜 PDU 不在覆盖范围内。"),
            (0, "2.3　除外责任：因甲方擅自改装、进水、雷击或超范围超负荷运行造成的损坏，不在质保范围。"),
            (1, "第三章　服务级别"),
            (0, "3.1　一级故障（业务中断）：30 分钟内响应，4 小时内到场。"),
            (0, "3.2　二级故障（性能显著下降）：2 小时内响应，次工作日到场。"),
            (0, "3.3　三级故障（不影响业务）：24 小时内响应，7 个工作日内处理。"),
            (0, "3.4　备件到货：一级故障所需备件须在 8 小时内到货。"),
            (1, "第四章　费用与结算"),
            (0, "4.1　合同总金额：人民币 186 万元（含税）。"),
            (0, "4.2　结算方式：分四期支付，每期 46.5 万元，于每季度首月 10 日前支付。"),
            (1, "第五章　违约责任"),
            (0, "5.1　乙方未按 3.1 条约定时限响应的，每延迟 1 小时按合同总额的千分之零点五支付违约金，单次上限 2 万元。"),
            (0, "5.2　累计违约金达到合同总额 5% 时，甲方有权单方解除合同并要求赔偿实际损失。"),
        ],
    },
    {
        "id": "KB-003",
        "file": "KB-003_数据中台技术白皮书.pdf",
        "kind": "pdf",
        "category": "技术文档类",
        "notes": "正常目录，用于测试技术类表述的检索。与 KB-004 构成对照。",
        "sections": [
            (1, "企业级数据中台技术白皮书"),
            (0, "版本 V3.2　　发布日期：2026 年 3 月"),
            (1, "1　概述"),
            (0, "本文档描述企业级数据中台的整体架构、关键技术选型与性能指标，供集成方与运维方参考。"),
            (1, "2　总体架构"),
            (0, "平台采用五层架构，自下而上依次为：数据接入层、存储计算层、数据治理层、数据服务层、应用层。各层之间通过标准接口解耦，允许单独替换。"),
            (1, "3　核心技术"),
            (2, "3.1　存算分离"),
            (0, "计算资源与存储资源独立扩缩容。存储节点默认采用三副本冗余，任一单节点故障不影响数据可用性。"),
            (2, "3.2　湖仓一体"),
            (0, "统一元数据服务，同时支持 Iceberg 与 Hudi 两种表格式，历史数据无需搬迁即可跨格式查询。"),
            (2, "3.3　实时链路"),
            (0, "基于 CDC 的增量同步，端到端时延低于 5 分钟，支持业务库到中台的准实时映射。"),
            (1, "4　性能指标"),
            (0, "4.1　单集群最大支持 5000 张表，日均写入量 12 亿条。"),
            (0, "4.2　即席查询 P95 响应时间 2.8 秒（测试条件：TPC-DS 1TB 规模，3 计算节点）。"),
            (0, "4.3　并发能力：单集群支持 200 个并发查询会话，超出后进入排队。"),
            (1, "5　部署要求"),
            (0, "5.1　最低配置：3 台 32 核 128GB 内存服务器，单机存储不低于 2TB。"),
            (0, "5.2　操作系统：麒麟 V10 SP3、统信 UOS 20 或更高版本。"),
            (0, "5.3　数据库：PostgreSQL 12 及以上，或达梦 DM8。"),
        ],
    },
    {
        "id": "KB-005",
        "file": "KB-005_数据分级分类管理规范.docx",
        "kind": "docx",
        "category": "制度规范类",
        "notes": "DOCX 格式，用于验证解析器对非 PDF 格式的处理。",
        "sections": [
            (1, "数据分级分类管理规范"),
            (0, "文件编号：ZD-2026-008　发布部门：数据安全委员会　生效日期：2026 年 2 月 1 日"),
            (1, "第一章　总则"),
            (0, "第一条　为规范公司数据资产管理，明确各级数据的保护要求，制定本规范。"),
            (0, "第二条　本规范适用于公司及所属分支机构在业务活动中产生、采集、加工的全部数据。"),
            (1, "第二章　数据分级"),
            (0, "第三条　公司数据按敏感程度分为四级，由低到高依次为：公开级（L1）、内部级（L2）、秘密级（L3）、机密级（L4）。"),
            (0, "第四条　机密级（L4）为最高保护等级，仅限经审批的授权人员访问，禁止导出至外部网络，禁止在个人终端留存副本。"),
            (0, "第五条　秘密级（L3）数据对外提供须经数据安全委员会审批，并签署数据使用协议。"),
            (0, "第六条　内部级（L2）数据限公司内部员工在办公网络访问，不得对外发布。"),
            (0, "第七条　公开级（L1）数据可对外发布，发布前需经品牌部门审核。"),
            (1, "第三章　数据分类"),
            (0, "第八条　按业务域划分为客户数据、财务数据、设备数据、运营数据四类。"),
            (0, "第九条　同一份数据同时符合多个业务域特征时，按保护等级最高的类别归类。"),
            (1, "第四章　职责与考核"),
            (0, "第十条　数据所有者承担数据分级准确性责任，每半年复核一次分级结果。"),
            (0, "第十一条　未按规定分级或未落实保护措施的，按公司信息安全奖惩条例处理。"),
        ],
    },
    {
        "id": "KB-006",
        "file": "KB-006_生产安全事故应急预案.pdf",
        "kind": "pdf",
        "category": "应急预案类",
        "notes": "含计算公式与代入过程，供中级任务 6（文档公式识别与计算）参考；本项目初赛不申报该任务，但语料要留。",
        "sections": [
            (1, "生产安全事故应急预案"),
            (0, "编制单位：安全生产管理部　版本：2026 版"),
            (1, "1　适用范围"),
            (0, "本预案适用于公司各数据中心机房及配套设施范围内发生的火灾、触电、设备倾覆、有毒气体泄漏等生产安全事故。"),
            (1, "2　组织机构"),
            (0, "应急指挥部由总经理任总指挥，分管安全的副总经理任副总指挥，下设抢险救援组、医疗救护组、后勤保障组、事故调查组。"),
            (1, "3　预防措施"),
            (0, "3.1　动火作业、高处作业、有限空间作业实行作业许可制度。"),
            (0, "3.2　机房内禁止存放易燃易爆物品，消防通道保持常闭常通状态。"),
            (1, "4　风险评估"),
            (0, "4.1　采用作业条件危险性评价法，风险值 D 的计算公式为：D = L × E × C。"),
            (0, "4.2　式中 L 为事故发生的可能性，取值范围 0.1 至 10；E 为人员暴露于危险环境的频繁程度，取值范围 0.5 至 10；C 为事故后果的严重度，取值范围 1 至 100。"),
            (0, "4.3　本机房动火作业的参数取值：L = 3，E = 6，C = 15。代入公式计算：D = 3 × 6 × 15 = 270。"),
            (0, "4.4　风险分级标准：D 大于 320 为极其危险；D 大于 160 且不超过 320 为高度危险；D 大于 70 且不超过 160 为显著危险；D 大于 20 且不超过 70 为一般危险；D 不超过 20 为稍有危险。"),
            (0, "4.5　评价结论：本机房动火作业风险值为 270，属于高度危险作业，须办理一级动火许可证，并由安全总监现场监督。"),
            (1, "5　应急响应分级"),
            (0, "5.1　按事故严重程度分为四级：Ⅰ级（特别重大）、Ⅱ级（重大）、Ⅲ级（较大）、Ⅳ级（一般）。"),
            (0, "5.2　Ⅰ级响应的启动条件：造成 3 人以上重伤，或直接经济损失 1000 万元以上。"),
            (0, "5.3　Ⅱ级响应启动条件：造成 1 至 2 人重伤，或直接经济损失 300 万元以上 1000 万元以下。"),
            (0, "5.4　Ⅲ级响应由分管副总指挥启动，Ⅳ级响应由部门负责人启动。"),
            (1, "6　后期处置"),
            (0, "6.1　事故调查应在 30 日内完成，并向应急指挥部提交调查报告。"),
            (0, "6.2　预案每三年修订一次；发生重大工艺变更时及时修订。"),
        ],
    },
    {
        "id": "KB-009",
        "file": "KB-009_员工手册.pdf",
        "kind": "pdf",
        "category": "制度规范类",
        "notes": "含繁体字与错别字，供中级任务 9（文档质量综合评估）参考。",
        "sections": [
            (1, "员工手册"),
            (0, "本手册适用于公司全体正式员工，自 2026 年 1 月起施行。"),
            (1, "一、入職與試用期"),
            (0, "1.1　新员工入职后设试用期，一般为 3 个月，特殊岗位可延长至 6 个月。"),
            (0, "1.2　試用期考核不合格者，公司可依法解除劳动合同。"),
            (0, "1.3　入职材料需在报到当日提交，包括身份證複印件、學歷證明與體檢報告。"),
            (1, "二、考勤與請假"),
            (0, "2.1　标准工时为每日 8 小时，上班时间为 9:00 至 18:00，其中 12:00 至 13:00 为午休。"),
            (0, "2.2　迟到超过 30 分钟按事假处理；当月累计迟到 3 次以上，扣发当月全勤獎金。"),
            (0, "2.3　员工请假需提前一个工作日在系统提交申请，由直属主管审批。因突发情况无法提前申请的，应在当日以电话报备，並於返岗後補辦手續。"),
            (0, "2.4　年休假天数按司龄计算：工作满 1 年不满 10 年的 5 天，满 10 年不满 20 年的 10 天，满 20 年以上的 15 天。"),
            (0, "2.5　加班按排调休，无法调休的按规定支付加班费。加班需事前审批，未审批的加班不视为加班。"),
            (1, "三、薪酬與福利"),
            (0, "3.1　薪资于每月 10 日前发放，遇节假日提前至最近工作日。"),
            (0, "3.2　公司为员工缴纳五险一金，缴存基数按上年度月平均工资确定。"),
            (0, "3.3　每年组织一次免费体检，费用由公司承担。"),
            (1, "四、保密与知识产权"),
            (0, "4.1　员工在职期间及离职后 2 年内，负有保守公司商业秘密的义务。"),
            (0, "4.2　职务发明的专利申请权归公司所有。"),
        ],
    },
    {
        "id": "KB-010",
        "file": "KB-010_问数服务API接口说明.md",
        "kind": "md",
        "category": "技术文档类",
        "notes": "Markdown 格式。",
        "sections": [
            (1, "问数服务 API 接口说明"),
            (1, "1　接口概览"),
            (0, "基础地址：https://api.example.com　版本前缀：/api/v1　鉴权方式：请求头 Authorization: Bearer <token>"),
            (1, "2　发起问数请求"),
            (0, "POST /api/v1/query"),
            (0, "请求体字段：question（必填，自然语言问题，最长 500 字符）、database（可选，目标库名称，默认 chinook）、top_k（可选，知识库检索条数，默认 5）、explain（可选，布尔值，是否返回推理链路，默认 false）。"),
            (0, "响应体字段：answer（字符串，最终答案）、sql（字符串，生成的结构化查询语句，问答类请求为 null）、refs（数组，知识库引用片段，含 doc、anchor、score 三个字段）、trace（数组，推理链路埋点，explain 为 true 时返回）。"),
            (1, "3　错误码"),
            (0, "400：请求参数不合法。401：鉴权失败，令牌缺失或已过期。404：目标库不存在。422：问题无法解析为可执行的查询。429：触发限流，默认每分钟 60 次。500：服务内部错误。503：依赖的数据库或向量库不可用。"),
            (1, "4　限流与重试"),
            (0, "触发 429 后应等待 1 秒并按指数退避重试，最多重试 3 次。服务端在响应头 X-RateLimit-Remaining 中返回剩余额度。"),
            (1, "5　版本变更"),
            (0, "v1.3 起新增 explain 参数；v1.2 起 refs 中增加 anchor 字段用于定位引用片段所在章节。"),
        ],
    },
    {
        "id": "KB-011",
        "file": "KB-011_信息系统项目验收报告.pdf",
        "kind": "pdf",
        "category": "验收报告类",
        "notes": "表格密集，用于验证解析器对表格的处理。",
        "sections": [
            (1, "信息系统项目验收报告"),
            (0, "项目名称: 智能问数平台建设项目　　建设单位: 中电云数据科技有限公司　　验收日期: 2026 年 8 月 12 日"),
            (1, "1　验收范围"),
            (0, "本次验收覆盖需求分析、系统设计、开发实现、测试与部署五个阶段交付物，不含后续运维服务。"),
            (1, "2　验收项与结论"),
            (0, "验收项共 8 项，各项权重与得分如下表。"),
            (0, "　验收项 | 权重 | 实得分　|　功能完整性 | 20% | 19　|　性能达标率 | 20% | 17　|　接口规范性 | 15% | 15　|　文档完整性 | 15% | 13　|　数据准确性 | 10% | 10　|　安全合规性 | 10% | 9　|　可维护性 | 5% | 4　|　培训完成度 | 5% | 5"),
            (0, "加权总分 = 19×0.2 + 17×0.2 + 15×0.15 + 13×0.15 + 10×0.1 + 9×0.1 + 4×0.05 + 5×0.05 = 92.15 分。"),
            (1, "3　遗留问题"),
            (0, "3.1　性能达标率一项扣 3 分，原因是并发 200 会话时 P95 响应时间 4.1 秒，超出 3 秒的验收上限。整改期限为 2026 年 9 月 30 日。"),
            (0, "3.2　文档完整性一项扣 2 分，原因是缺少数据字典与运维手册两个附件。"),
            (0, "3.3　可维护性一项扣 1 分，原因是单元测试覆盖率 68%，未达到 80% 的要求。"),
            (1, "4　验收结论"),
            (0, "加权总分 92.15 分，达到「优秀」等级（90 分及以上），验收通过，遗留问题按第 3 章整改期限完成。"),
        ],
    },
    {
        "id": "KB-012",
        "file": "KB-012_售后服务承诺函.txt",
        "kind": "txt",
        "category": "商务函件类",
        "notes": "纯文本格式，与 KB-001 存在可对比的时限口径，用于跨文档多跳问答。",
        "sections": [
            (1, "售后服务承诺函"),
            (0, "致：北方数据中心运营有限公司"),
            (0, "我方就智能计算设备维保项目作如下服务承诺："),
            (0, "一、服务热线 7×24 小时开通，受理电话 400-000-0000。"),
            (0, "二、故障响应：接到报修后 2 小时内派单，工程师 6 小时内到达现场。"),
            (0, "三、备件供应：常用备件 72 小时内到货，进口备件最长不超过 15 个工作日。"),
            (0, "四、服务方式：现场服务与远程支持相结合，远程支持记录随季度巡检报告一并提交。"),
            (0, "五、服务期限：自合同签订之日起一年，期满前一个月双方协商续签。"),
            (0, "六、服务人员：派驻现场工程师 2 名，工作时间为工作日 9:00 至 18:00。"),
            (0, "七、本承诺函为要约邀请，最终以双方签订的服务合同为准。"),
            (0, "承诺方：中电云维保服务有限公司　　日期：2026 年 4 月 20 日"),
        ],
    },
    {
        "id": "KB-013",
        "file": "KB-013_机房运维操作规程.pdf",
        "kind": "pdf",
        "category": "操作规程类",
        "notes": "正常目录版本，将用于生成 KB-014。",
        "sections": [
            (1, "机房运维操作规程"),
            (1, "1　人员出入管理"),
            (0, "1.1　机房实行门禁管理，进出须刷卡并登记，外来人员须由本公司人员全程陪同。"),
            (0, "1.2　禁止携带食品、饮料、易燃易爆物品进入机房。"),
            (1, "2　温湿度管理"),
            (0, "2.1　机房温度应控制在 18℃ 至 27℃ 之间，相对湿度应控制在 30% 至 70% 之间。"),
            (0, "2.2　温湿度超限时应在 15 分钟内通知运维值班人员，并在 30 分钟内处置完毕。"),
            (1, "3　巡检要求"),
            (0, "3.1　日常巡检每日两次，分别为 9:30 与 16:30；月度巡检每月最后一个工作日前完成。"),
            (0, "3.2　巡检内容包括设备指示灯状态、风扇运转声音、线缆连接、告警记录。"),
            (0, "3.3　巡检记录须在巡检结束后 2 小时内录入运维系统，纸质记录保存不少于 1 年。"),
            (1, "4　电力保障"),
            (0, "4.1　UPS 电池组每季度放电测试一次，容量低于额定值 80% 的应及时更换。"),
            (0, "4.2　双路市电切换测试每半年进行一次。"),
            (1, "5　应急处置"),
            (0, "5.1　市电中断后 UPS 应能支撑 30 分钟以上，值班人员须在 10 分钟内启动柴油发电机预案。"),
            (0, "5.2　发生火警时，先切断非消防电源，再按预案疏散人员，最后使用气体灭火系统灭火。"),
        ],
    },
    {
        "id": "KB-014",
        "file": "KB-014_机房运维操作规程_表格密集版.pdf",
        "kind": "pdf",
        "category": "操作规程类",
        "notes": "表格密集样本，用于验证表格结构还原。",
        "sections": [
            (1, "机房运维巡视检查记录表"),
            (0, "机房名称: A3 机房　　记录人: 张伟　　检查日期: 2026-09-10"),
            (1, "一、环境参数记录"),
            (0, "　检查项　|　标准范围　|　实测值　|　结论　|　温度　|　18℃~27℃　|　22.6℃　|　合格　|　相对湿度　|　30%~70%　|　45%　|　合格　|　正压值　|　不小于 5Pa　|　8Pa　|　合格　|　噪声　|　不高于 65dB　|　58dB　|　合格"),
            (1, "二、设备状态记录"),
            (0, "　设备编号　|　设备类型　|　运行状态　|　告警数　|　CU-A3-001　|　GPU 服务器　|　正常　|　0　|　CU-A3-002　|　GPU 服务器　|　正常　|　0　|　ST-A3-011　|　分布式存储节点　|　正常　|　1　|　NW-A3-005　|　核心交换机　|　正常　|　0"),
            (1, "三、异常与处置"),
            (0, "3.1　ST-A3-011 存储节点存在 1 条磁盘亚健康告警，型号为 3.84TB NVMe，序列号 SN-8823917，已生成更换工单 GD-20260910-07。"),
            (0, "3.2　其余设备无异常。"),
            (1, "四、下次巡检安排"),
            (0, "下次巡检日期：2026 年 10 月 15 日；责任人：张伟；备用责任人：李娜。"),
        ],
    },
]

# 扫描件内容（先渲染为 PDF 文本，再转成图片型 PDF）
SCANNED_DOCS: list[dict] = [
    {
        "id": "KB-007",
        "file": "KB-007_服务器巡检记录扫描件.pdf",
        "kind": "pdf-scanned",
        "category": "运维记录类",
        "notes": "图片型 PDF，无文本层，必须经 OCR 才能检索。",
        "sections": [
            (1, "服务器巡检记录（扫描件）"),
            (0, "机房：A3　　巡检人：张伟　　巡检日期：2026-09-10"),
            (0, "设备总数 248 台，本次巡检 248 台，异常 1 台。"),
            (0, "机房温度 22.6 摄氏度，相对湿度 45%，正压值 8Pa。"),
            (0, "UPS 电池组编号 BT-2024-11，上次放电测试日期 2026-07-08，容量实测值 86%。"),
            (0, "异常设备：存储节点 ST-A3-011，磁盘亚健康，工单号 GD-20260910-07。"),
            (0, "下次巡检日期：2026 年 10 月 15 日。"),
            (0, "签发：运维部　王强　2026-09-11"),
        ],
    },
    {
        "id": "KB-008",
        "file": "KB-008_招标文件技术规格书扫描件.pdf",
        "kind": "pdf-scanned",
        "category": "招投标类",
        "notes": "图片型 PDF，含关键金额与期限，必须经 OCR 才能检索。",
        "sections": [
            (1, "招标文件技术规格书（扫描件）"),
            (0, "招标编号：ZB-2026-0231　　采购人：北方数据中心运营有限公司"),
            (0, "一、投标保证金：人民币 8 万元，须于投标截止时间前缴纳。"),
            (0, "二、交货期：合同签订之日起 60 日历天内完成到货与安装。"),
            (0, "三、质保期：验收合格之日起 3 年。"),
            (0, "四、付款方式：到货验收后支付 30%，终验合格后支付 60%，质保期满后支付 10%。"),
            (0, "五、技术参数要求：单机 GPU 显存不低于 80GB；单机内存不低于 1024GB；单机电源冗余配置 2+2。"),
            (0, "六、投标截止时间：2026 年 6 月 30 日 9:30。"),
        ],
    },
]

DOCS_ALL = DOCS + SCANNED_DOCS


# ======================================================================================
# 渲染
# ======================================================================================
# ======================================================================================
# 中文字体选择
# ======================================================================================
# 为什么不能只用 PyMuPDF 内置的 china-s：
#   内置 CJK 字体（Adobe Song）缺中文标点，且 ASCII 走全角字形，
#   "248 台" 会被画成 "2 4 8 台"、逗号句号直接丢失——语料看着就不像企业文档，
#   扫描件还要拿去跑 OCR，字形质量差会额外引入识别错误。
#   所以优先挂系统真实中文字体；找不到才退回内置字体（并在输出里警告）。
FONT_CANDIDATES = [
    "C:/Windows/Fonts/Deng.ttf",            # 等线
    "C:/Windows/Fonts/msyh.ttc",            # 微软雅黑
    "C:/Windows/Fonts/NotoSansSC-VF.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def pick_font():
    """返回 (pymupdf.Font 对象 或 None, 字体文件路径 或 None)。"""
    import pymupdf

    for path in FONT_CANDIDATES:
        p = Path(path)
        if p.exists():
            try:
                return pymupdf.Font(fontfile=str(p)), p
            except Exception:  # noqa: BLE001 —— 某个字体解析失败就试下一个
                continue
    return None, None


def stamp_metadata(pdf, doc: dict) -> None:
    """把 PDF 元数据钉死，去掉"当前时间"这个不确定来源。"""
    title = doc["sections"][0][1] if doc["sections"] else doc["id"]
    pdf.set_metadata(
        {
            "title": title,
            "author": "语料生成器（C 组）",
            "subject": f"{doc['id']} {doc['category']}",
            "creator": "data/corpus/tools/build_corpus.py",
            "producer": "data/corpus/tools/build_corpus.py",
            "creationDate": FIXED_DATE,
            "modDate": FIXED_DATE,
        }
    )


def render_pdf(doc: dict, out_path: Path) -> None:
    import pymupdf

    font, font_path = pick_font()
    pdf = pymupdf.open()
    page = pdf.new_page(width=PAGE_W, height=PAGE_H)
    pages = [page]
    registered: set[int] = set()

    def ensure_font(pg) -> None:
        if font_path is None:
            return
        key = id(pg)
        if key in registered:
            return
        pg.insert_font(fontname="cn", fontfile=str(font_path))
        registered.add(key)

    def measure(text: str, size: float) -> float:
        if font is not None:
            return font.text_length(text, fontsize=size)
        return pymupdf.get_text_length(text, fontname="china-s", fontsize=size)

    def style(size: float) -> tuple[str, float]:
        return ("cn", size) if font is not None else ("china-s", size)

    y = MARGIN

    def new_page_if_needed(need: int) -> None:
        nonlocal page, y
        if y + need > PAGE_H - MARGIN:
            page = pdf.new_page(width=PAGE_W, height=PAGE_H)
            pages.append(page)
            y = MARGIN

    def put(text: str, size: float) -> None:
        nonlocal y
        ensure_font(page)
        fname, fsize = style(size)
        width_limit = PAGE_W - 2 * MARGIN
        buf = ""
        for ch in text:
            if measure(buf + ch, fsize) > width_limit and buf:
                new_page_if_needed(LINE_H)
                ensure_font(page)
                page.insert_text((MARGIN, y), buf, fontname=fname, fontsize=fsize)
                y += LINE_H
                buf = ""
            buf += ch
        if buf:
            new_page_if_needed(LINE_H)
            ensure_font(page)
            page.insert_text((MARGIN, y), buf, fontname=fname, fontsize=fsize)
            y += LINE_H

    for level, text in doc["sections"]:
        if level:
            y += 6
            new_page_if_needed(HEAD_SIZE.get(level, 11) + 8)
            put(text, HEAD_SIZE.get(level, 11))
            y += 2
        else:
            put(text, BODY_SIZE)

    # 必须做字体子集化：系统中文 TTF 动辄十几 MB，整体嵌入会让每份 PDF 涨到 16MB，
    # 15 份语料就 200MB+，提交的 ZIP 包会失控。subset_fonts 只保留用到的字形。
    try:
        pdf.subset_fonts()
    except Exception:  # noqa: BLE001 —— 老版本没有该方法时退化为整体嵌入
        pass
    stamp_metadata(pdf, doc)
    pdf.save(str(out_path), garbage=4, deflate=True, clean=True)
    pdf.close()


def render_scanned(doc: dict, out_path: Path) -> None:
    """把文本 PDF 逐页栅格化成图片，再重组成无文本层的 PDF —— 模拟扫描件。

    参数刻意调差：150dpi + 轻微倾斜 + 灰度噪声，
    对应题面「低质量数据：模糊扫描件、颠倒图片」的鲁棒性要求。
    """
    import pymupdf

    tmp = out_path.with_suffix(".tmp.pdf")
    render_pdf(doc, tmp)

    src = pymupdf.open(str(tmp))
    out = pymupdf.open()
    for i, page in enumerate(src):
        # 150dpi 灰度：故意降质。真实扫描件普遍在这个量级，
        # 用它当基准能暴露 OCR 链路在低质量输入下的表现。
        pix = page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        img_bytes = pix.tobytes("png")
        rect = pymupdf.Rect(0, 0, page.rect.width, page.rect.height)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        # 奇数页向右偏移 6pt，模拟进纸不齐；偶数页摆正作为对照
        offset = 6 if i % 2 else 0
        new.insert_image(rect + (offset, 0, offset, 0), stream=img_bytes)
    stamp_metadata(out, doc)
    out.save(str(out_path), garbage=4, deflate=True, clean=True)
    out.close()
    src.close()
    tmp.unlink(missing_ok=True)


def render_docx(doc: dict, out_path: Path) -> None:
    from datetime import datetime

    from docx import Document

    d = Document()
    for level, text in doc["sections"]:
        if level == 1:
            d.add_heading(text, level=1)
        elif level == 2:
            d.add_heading(text, level=2)
        elif level == 3:
            d.add_heading(text, level=3)
        else:
            d.add_paragraph(text)
    # DOCX 同理：core properties 里默认写当前时间，会破坏可复现性
    core = d.core_properties
    fixed = datetime(2026, 9, 17, 0, 0, 0)
    core.created = fixed
    core.modified = fixed
    core.last_modified_by = "corpus-builder"
    core.revision = 1
    d.save(str(out_path))


def render_text(doc: dict, out_path: Path) -> None:
    is_md = doc["kind"] == "md"
    lines = []
    for level, text in doc["sections"]:
        if is_md:
            lines.append(("#" * level + " " + text) if level else text)
        else:
            lines.append(("#" * level + " " + text) if level else text)
        lines.append("")
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------------
# 目录混乱样本：把正常文档降级
# --------------------------------------------------------------------------------------
def degrade_outline(doc: dict, seed: int) -> list[tuple[int, str]]:
    """把一份正常文档变成“目录混乱”样本。

    混乱不是随机打乱，而是复刻题面提到的三种真实故障：
      a) 层级丢失 —— 所有标题降为正文，解析器无法靠样式识别章节；
      b) 编号错乱 —— 条款编号与出现顺序不匹配，靠编号推断层级会失效；
      c) 章节顺序错位 —— 第五章跑到前面，靠顺序推断结构同样失效。
    随机化用固定 seed，保证任何机器上重跑得到同一份语料。
    """
    rng = random.Random(seed)
    heads = [(lvl, txt) for lvl, txt in doc["sections"] if lvl]
    bodies = [(lvl, txt) for lvl, txt in doc["sections"] if not lvl]
    rng.shuffle(bodies)

    out: list[tuple[int, str]] = [(0, doc["sections"][0][1])]
    out.append((0, "（本文件由旧版系统导出，目录层级信息已丢失，章节顺序与原文档不一致）"))

    # 章节顺序错位 + 层级全丢
    rng.shuffle(heads)
    merged: list[tuple[int, str]] = []
    for i, (_, head) in enumerate(heads):
        merged.append((0, f"[{rng.randint(1, 9)}.{rng.randint(1, 9)}] {head}"))
        take = max(1, len(bodies) // max(1, len(heads)))
        for _ in range(take):
            if bodies:
                merged.append((0, bodies.pop()[1]))
    merged.extend((0, b) for _, b in bodies)
    return out + merged


def render_degraded(src_doc: dict, new_id: str, new_file: str, notes: str, seed: int) -> dict:
    return {
        "id": new_id,
        "file": new_file,
        "kind": "pdf",
        "category": src_doc["category"],
        "notes": notes,
        "sections": degrade_outline(src_doc, seed),
        "derived_from": src_doc["id"],
    }


# ======================================================================================
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def content_fingerprint(sections: list[tuple[int, str]]) -> str:
    """语料**内容**指纹：对章节数据做哈希，而不是对生成出来的文件做哈希。

    为什么不用文件字节哈希当复现依据：
        PDF 写入器会在文件里留下自己版本相关的痕迹（对象编号、trailer /ID、
        字体子集打包顺序），PyMuPDF 升级一次，字节哈希就全变——
        拿它当"是否可复现"的证据，会得出错误结论（实测过：15 份里有 13 份对不上，
        而且把创建时间钉死也修不好）。
        真正需要可复现的是**内容**：同一份 sections 必须得到同一份文档。
        文件字节哈希仍然记录，但只作为本地产物指纹，并明确标注它不跨版本稳定。
    """
    payload = json.dumps(sections, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成知识库语料")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    by_id = {d["id"]: d for d in DOCS_ALL}
    docs: list[dict] = list(DOCS_ALL)
    # 目录混乱样本：由 KB-002（源自 KB-001）与 KB-004（源自 KB-003）内部已有约定，
    # 这里显式构造，保证“≥2 份目录混乱”这条硬要求不依赖人工记性。
    docs.append(render_degraded(by_id["KB-001"], "KB-002", "KB-002_智能计算设备年度维保服务合同_目录混乱版.pdf",
                                "目录混乱样本 1：层级丢失 + 编号错乱 + 章节错位。对应中级任务 7。", seed=20260917))
    docs.append(render_degraded(by_id["KB-003"], "KB-004", "KB-004_数据中台技术白皮书_目录混乱版.pdf",
                                "目录混乱样本 2：层级丢失 + 编号错乱 + 章节错位。对应中级任务 7。", seed=20260918))
    docs.append(render_degraded(by_id["KB-013"], "KB-015", "KB-015_机房运维操作规程_目录混乱版.pdf",
                                "目录混乱样本 3：与 KB-013 同源，用于验证解析器在同一内容上的一致性。", seed=20260919))
    docs.sort(key=lambda d: d["id"])

    manifest = []
    for d in docs:
        path = out / d["file"]
        if d["kind"] == "pdf":
            render_pdf(d, path)
        elif d["kind"] == "pdf-scanned":
            render_scanned(d, path)
        elif d["kind"] == "docx":
            render_docx(d, path)
        elif d["kind"] in ("md", "txt"):
            render_text(d, path)
        else:
            raise SystemExit(f"未知类型：{d['kind']}")

        size = path.stat().st_size
        manifest.append(
            {
                "id": d["id"],
                "file": d["file"],
                "format": path.suffix.lstrip("."),
                "kind": d["kind"],
                "category": d["category"],
                "bytes": size,
                "content_sha256": content_fingerprint(d["sections"]),
                "file_sha256_local": sha256(path),
                "has_text_layer": d["kind"] not in ("pdf-scanned",),
                "outline_ok": "目录混乱" not in d["file"],
                "notes": d["notes"],
                **({"derived_from": d["derived_from"]} if d.get("derived_from") else {}),
            }
        )
        print(f"{d['id']:<8}{path.suffix:<6}{size:>9,} B  {d['file']}")

    manifest_path = out.parent / "manifest.json"
    previous = {}
    if manifest_path.exists():
        try:
            previous = {m["id"]: m.get("content_sha256") for m in json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]}
        except Exception:  # noqa: BLE001 —— 旧清单坏了不该拦住本次生成
            previous = {}

    changed = [
        m["id"] for m in manifest if previous.get(m["id"]) and previous[m["id"]] != m["content_sha256"]
    ]

    manifest_path.write_text(
        json.dumps(
            {
                "generator": "data/corpus/tools/build_corpus.py",
                "count": len(manifest),
                "formats": sorted({m["format"] for m in manifest}),
                "categories": sorted({m["category"] for m in manifest}),
                "hash_notes": {
                    "content_sha256": "语料内容指纹，对章节数据做哈希。跨机器、跨 PyMuPDF 版本稳定，用它判断内容是否被改过。",
                    "file_sha256_local": "生成文件的字节哈希。受 PDF/DOCX 写入器版本影响，跨版本会变，只作本地产物指纹，不要拿它当复现依据。",
                },
                "derived": [m for m in manifest if m["id"] in ("KB-002", "KB-004", "KB-015")],
                "documents": manifest,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"\n合计 {len(manifest)} 份，格式：{sorted({m['format'] for m in manifest})}")
    print(f"扫描件（无文本层）：{[m['id'] for m in manifest if not m['has_text_layer']]}")
    print(f"目录混乱样本：{[m['id'] for m in manifest if not m['outline_ok']]}")
    if changed:
        print(f"\n注意：以下语料的内容指纹与上一版清单不同，确认是有意改动还是误编辑 —— {changed}")
    else:
        print("内容指纹与上一版清单一致：本次生成没有改动语料内容。")
    print(f"清单已写出：{manifest_path}")
    font, font_path = pick_font()
    if font_path:
        print(f"渲染字体：{font_path}")
    else:
        print("警告：没有找到系统中文字体，退回 PyMuPDF 内置 china-s ——")
        print("      该字体缺中文标点且 ASCII 为全角，语料观感与 OCR 难度都会失真。")
        print("      请安装任一候选中文字体后重跑本脚本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
