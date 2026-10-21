# app/rag 包初始化。
#
# 这里只做一件事：**保证 .env 在任何 app.rag.* 子模块被导入前加载完成。**
#
# 为什么放在包初始化里：Python 导入 app.rag.retrieval.reranker 时，
# 会先执行 app -> app.rag -> app.rag.retrieval 各级 __init__，所以放这里
# 能覆盖全部入口（含 `python -m app.rag.retrieval.reranker` 这种直接运行）。
#
# 曾经的做法是"谁需要谁 import config"——只有 answer.py / llm.py 这么做了，
# 于是直接运行 reranker / indexer / corpus 时 .env 不加载：
# BGE_RERANKER_MODEL 读不到 -> 回退成 HF 仓库名 -> 缓存缺失时静默下载 2GB 权重，
# 表现为"长时间无输出"。别再把这条保证退回到靠个别模块自觉。
#
# config 模块在 import 期就执行 load_env_file()，这里 import 一次即完成初始化；
# 不显式调用 load_env_file()，避免重复加载。
from . import config as _config  # noqa: F401  触发一次配置初始化（import 即副作用）
