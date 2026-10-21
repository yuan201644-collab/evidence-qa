"""config.py - 结构化链路（nl2sql）读环境变量的统一入口。

为什么单独放一处、而不 import `app/rag/config.py`：
    项目约定「结构化链路对非结构化链路保持零 import 依赖」
    （docs/要素识别与改写_接口_v0.1.md §12.4）。这条纪律是有代价换来的：
    跨源工具当初 import 了 B 的 4 个 RAG 内部模块，B 一改内部接口就会静默搞坏它。
    所以这里自带一份最小实现，宁可重复也不要隐性耦合。

    ⚠️ 代价：本文件与 `app/rag/config.py` 有一份**语义相同**的实现（KEY=VALUE 解析、
    .env 空值按未设置处理、env_int 校验）。正确的中期做法是抽出一个中立的
    `app/env.py` 供两侧共用 —— 已记入意见箱，**不在本轮擅自改 B 的文件**。

变量名以 `.env.example` 为准（C 的约定）。即使没有 .env，每个读取点也带与
`.env.example` 同值的默认值，这样没人配 .env 时口径也不会悄悄跑偏。

    SCHEMA_META_PATH   库结构元数据文件（默认 <项目根>/infra/schema_meta.json）
    NL2SQL_DATABASE     业务库库名（默认 chinook）
    DATABASE_URL       业务库连接串（优先）；缺失时用 POSTGRES_* 拼
    SQL_DIALECT        方言（默认 postgres）
    SQL_READONLY_ONLY  是否强制只读（默认 true）
    SQL_AUTO_LIMIT     无 LIMIT 时自动补的行数上限（默认 1000；0 = 不补）
    SQL_LARGE_RESULT_MAX_ROWS  自动上限触顶后受控扩展的最大行数（默认 5000）
    RESULT_SET_CACHE_MAX_BYTES 结果集缓存序列化字节上限（默认 16 MiB）
    SQL_TIMEOUT_SECONDS 语句超时秒数（默认 30）
"""

from __future__ import annotations

import os
from datetime import datetime

#: .env 里成功写进 os.environ 的键（供入口脚本打印，便于排查"我配了却没生效"）
_DOTENV_LOADED = ()
#: .env 里写了但值为空的键（按"未设置"处理，见 load_env_file）
_EMPTY_KEYS = []


def load_env_file(path=None):
    """把 .env 里的 KEY=VALUE 装进 os.environ，**已存在的变量不覆盖**。返回加载到的键名。

    逐条语义与 `app/rag/config.load_env_file` 对齐（那份 docstring 有完整取舍说明）：
      - 只认 `KEY=VALUE`（允许前置 `export`）与 `#` 注释行；
      - 值两侧的成对引号剥掉（.env 常被写成 LLM_MODEL="xxx"，引号会被一起带进请求体）；
      - **空值视为未设置**：`KEY=` 不写进 os.environ，只登记进空值名单。
        理由：`os.environ.get(name, default)` 只在**键缺失**时返回 default，
        键存在但值是空串会返回空串 —— "照抄模板忘了填"就会表现成静默的错误行为，
        而不是"回落到默认值"。
    """
    if path is None:
        # app/nl2sql/config.py -> app/nl2sql -> app -> 项目根
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(here))
        path = os.path.join(root, ".env")
    if not os.path.isfile(path):
        return []

    loaded = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line == "" or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key == "" or key in os.environ:
                continue
            if value == "":
                if key not in _EMPTY_KEYS:
                    _EMPTY_KEYS.append(key)
                continue
            os.environ[key] = value
            loaded.append(key)
    return loaded


def loaded_dotenv_keys():
    """本进程从 .env 加载了哪些键。空列表说明没找到 .env，或里面的键都已被真实环境变量占着。"""
    return list(_DOTENV_LOADED)


def loaded_empty_keys():
    """`.env` 里写了但值为空的键 —— 入口脚本可用它提示"你配了这一行但没填值"。"""
    return list(_EMPTY_KEYS)


def env_str(name, default=""):
    """读字符串型环境变量：未设或空串取默认值。"""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def env_int(name, default, minimum=1):
    """读整数型环境变量：未设或空串取默认值；设了但非法**明确报错**，不静默回退。

    不静默回退的理由：把行数上限读成 0 的后果是"不报错但结果必然不可用"，
    属于最难查的一类问题，宁可在启动时炸掉。
    minimum 默认为 1（行数类）；允许 0 的场合（如"不补 LIMIT"）传 minimum=0。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("环境变量 " + name + " 必须是整数，当前值：" + repr(raw))
    if value < minimum:
        raise ValueError(
            "环境变量 " + name + " 必须 >= " + str(minimum) + "，当前值：" + repr(raw)
        )
    return value


def env_bool(name, default=False):
    """读布尔型环境变量。只认 1/true/yes/on（不分大小写），其余非空值一律当 False 并明确报错。

    为什么对"其余非空值"报错：写 `SQL_READONLY_ONLY=TRUE ` 这种带尾空格的能认，
    但写 `SQL_READONLY_ONLY=disabled` 的人多半是想关闭它 —— 若静默当成 False 反而是对的，
    可写 `SQL_READONLY_ONLY=none` 又可能被理解成"不做限制"。这类歧义在安全开关上
    **不能猜**，所以直接报错要求写清楚。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        "环境变量 " + name + " 必须是布尔字面量（1/0/true/false/yes/no/on/off），当前值：" + repr(raw)
    )


#: 可能出现在 DATABASE_URL 里的 SQLAlchemy 方言后缀。
#: psycopg3 直连**不认** `postgresql+psycopg://`，会直接报错；
#: 而 .env.example 里特意提醒过"A 侧用 SQLAlchemy 时不要改这一行" ——
#: 也就是说迟早会有人把带后缀的串写进来，这里主动剥掉。
_SQLALCHEMY_PREFIXES = ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres://")


def _strip_driver_suffix(dsn):
    for prefix in _SQLALCHEMY_PREFIXES:
        if dsn.startswith(prefix):
            return "postgresql://" + dsn[len(prefix):]
    return dsn


def get_dsn():
    """取业务库连接串：DATABASE_URL 优先，缺失时用 POSTGRES_* 拼出 chinook 的串。

    ⚠️ 拼串时库名用 `NL2SQL_DATABASE`（默认 chinook），**不用 POSTGRES_DB** ——
    POSTGRES_DB 在 compose 里是容器默认库（postgres），业务库 chinook 是
    initdb 脚本建的，两者不是一回事（见 infra/docker-compose.yml 的注释）。
    """
    dsn = env_str("DATABASE_URL")
    if dsn:
        return _strip_driver_suffix(dsn)

    user = env_str("POSTGRES_USER", "ict")
    password = env_str("POSTGRES_PASSWORD")
    host = env_str("POSTGRES_HOST", "127.0.0.1")
    port = env_str("POSTGRES_PORT", "5432")
    database = env_str("NL2SQL_DATABASE", "chinook")
    auth = user + (":" + password if password else "")
    return "postgresql://" + auth + "@" + host + ":" + port + "/" + database


# import 期即加载 .env：任何 import 本模块的调用方都无须记得手动调用一次。
_DOTENV_LOADED = tuple(load_env_file())


def time_anchor_from_env() -> datetime | None:
    """读 ``APP_TIME_ANCHOR``（ISO 日期，如 2024-06-15）作为时间基准；未设置返回 None（= 取系统当前时间）。

    为什么要有它：「去年 / 本月」是相对词，必须钉死基准跑分才可复现
    （docs/要素识别与改写_接口_v0.1.md §6.1 —— 评测基准已拍板为 2024-06-15）。
    解析失败要**报错**而不是静默回落：基准写错一格，所有时间用例的 gold 全体漂移，
    静默回落会让这种错以「跑分莫名变差」的形式出现，排查成本极高。
    """
    raw = os.environ.get("APP_TIME_ANCHOR", "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"APP_TIME_ANCHOR 不是合法的 ISO 日期：{raw!r}（示例：2024-06-15；留空表示用系统当前时间）"
        ) from exc


__all__ = [
    "load_env_file",
    "loaded_dotenv_keys",
    "loaded_empty_keys",
    "env_str",
    "env_int",
    "env_bool",
    "get_dsn",
    "time_anchor_from_env",
]
