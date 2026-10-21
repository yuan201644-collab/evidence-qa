"""config.py - RAG 链路读环境变量的统一入口

为什么单独放一处：
    llm.py（超时/重试次数）和 answer.py（召回深度/最终条数）都要读整数型环境变量，
    各写一套校验迟早会漂移。这里只放"读 + 校验"，不引入配置对象、不做集中注册，
    保持各模块自己声明默认值的就近可读性。

.env 加载：
    本模块 import 时会把项目根目录的 .env 装进 os.environ（**已存在的变量不覆盖**）。
    为什么要有这一步：在此之前 app 代码完全不读 .env（只有 infra/ 下的脚本自带 loader），
    于是"我把 key 写进 .env 了"和"程序读不到"之间裂了一道无声的缝，
    表现为鉴权失败或静默走默认值，很难看出来。
    解析方式是手写的 KEY=VALUE + # 注释，不引入 python-dotenv
    （照 infra/scripts/export_schema_meta.py:51 的做法）。

    ⚠️ 加载时机必须覆盖**所有**入口，不能只靠个别模块碰巧 import 本模块。
    曾经只有 answer.py / llm.py import 了它，于是 `python -m app.rag.retrieval.reranker`
    这类直接运行不加载 .env：BGE_RERANKER_MODEL 读不到 -> 回退成 HF 仓库名
    -> 缓存缺失时静默开始下载 2GB 权重。所以现在由 app/rag/__init__.py 统一触发。

约定：
    变量名以 .env.example 为准（C 的 infra 约定）。
    即使 .env 不存在，每个读取点也都带与 .env.example 同值的默认值，
    这样没人配 .env 时口径也不会悄悄跑偏。
"""

import os

_DOTENV_LOADED = ()
#: .env 里写了但值为空的键（按"未设置"处理，见 load_env_file 的 docstring）。
_EMPTY_KEYS = []


def load_env_file(path=None):
    """把 .env 里的 KEY=VALUE 装进 os.environ，已存在的变量不覆盖。返回加载到的键名。

    只认两种行：`KEY=VALUE`（允许前面写 export）与 `#` 注释。
    值两边的成对引号会剥掉，因为 .env 常被写成 LLM_MODEL="xxx" 的形式，
    而带引号的模型名会把引号一起带进请求体。

    **空值视为未设置**：`KEY=` 这类行不写进 os.environ，只记进空值名单。
    为什么不能落进环境：`os.environ.get(name, default)` 只在**键不存在**时返回
    default，键存在但值是空串会返回空串，后果是 --
        BGE_RERANKER_MODEL=   -> CrossEncoder("") 报 Repo id must be in the form ...
        HF_ENDPOINT=          -> httpx 抛 UnsupportedProtocol
    这正是"照抄了模板却忘了填"最容易撞上的形态，所以按没配处理，让下游走默认值。
    """
    if path is None:
        # app/rag/config.py -> app/rag -> app -> 项目根
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
                # 空值按"未设置"处理（理由见 docstring），只登记不写入
                if key not in _EMPTY_KEYS:
                    _EMPTY_KEYS.append(key)
                continue
            os.environ[key] = value
            loaded.append(key)
    return loaded


def loaded_empty_keys():
    """`.env` 里写了但值为空的键。

    入口脚本可用它提示"你配了这一行但没填值，当前按未设置处理"。
    """
    return list(_EMPTY_KEYS)


def loaded_dotenv_keys():
    """本进程启动时从 .env 加载了哪些键。

    给入口脚本打印用：空元组说明没找到 .env，或者里面的键都已被真实环境变量占着。
    "我明明配了却没生效"这类疑问，看一眼这行输出就能定位。
    """
    return list(_DOTENV_LOADED)


# 在 import 期就把 .env 装好：**任何 app.rag.* 子模块导入都会先执行 app/rag/__init__.py，
# 而它在同样的时机 import 本模块**，所以不需要每个模块各自记得调用一次（少一处能忘的地方）。
_DOTENV_LOADED = tuple(load_env_file())


def env_int(name, default, minimum=1):
    """读整数型环境变量：未设或空串取默认值；设了但非法则明确报错。

    不静默回退。把条数读成 0 的后果是链路"不报错但结果必然不可用"
    （例如空上下文直接送给 LLM），属于最难查的一类问题，宁可在启动时炸掉。

    minimum 默认 1（条数类）；重试次数这类允许 0 的传 minimum=0。
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
