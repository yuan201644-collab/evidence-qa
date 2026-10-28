# -*- coding: utf-8 -*-
"""事实判定扩展层（方向三 · KAG 适配补丁）。

这个包有两种运行环境，一份代码两处都用：

1. **容器内**（`<site-packages>/feva_override/`）：随 KAG 启动，`__init__` 里注册
   三个覆盖模块（prompt / executor / prompt_dag），`refinject` 另挂生成钩子。
   启用方式是新增 pipeline yaml 指向覆盖后的 planner 类型；
   回滚删目录 + 删 yaml 即可，不改任何上游文件。

2. **交付应用内**（`contrib/kag/feva_override/`）：被 `app/kag_ask.py` 按文件路径
   加载，只用 `facts` / `ledger` 两个纯计算模块，**不 import kag**。
   因此注册段必须能在没有 kag 的环境里静默跳过。

两条路径共用同一份 `facts.py` / `ledger.py`：容器里生成注入表、应用里重算展示表，
算法必须逐字一致，否则「界面上看到的表」与「模型当时看到的表」会对不上。
"""
__version__ = "1.2"

# 只有在容器里（能 import 到 kag）才注册覆盖模块。应用侧没有 kag，
# 跳过是正常分支，不该报错，也不该往 /tmp 写日志。
_IN_CONTAINER = True
try:
    import kag  # noqa: F401
except Exception:  # pragma: no cover - 应用侧的正常分支
    _IN_CONTAINER = False

if _IN_CONTAINER:
    # prompt / executor **不吞异常**：注册失败必须在冒烟阶段立刻暴露，
    # 而不是静默退化成基线。
    from feva_override import executor, prompt  # noqa: F401,E402

    # prompt_dag 依赖 kag.interface / kag.common.parser 更深的一层，在部分加载时机下
    # 可能触发循环导入。这里单独兜一层，但**必须留痕**：A3 臂的 E1 判据会核对注册名，
    # 若这里打印了 skipped，说明 A3 实际跑的是 A2 配置，该臂结果作废。
    try:
        from feva_override import prompt_dag  # noqa: F401,E402
    except Exception as _exc:  # pragma: no cover
        import os as _os
        import sys as _sys
        import time as _time
        _msg = "[pid=%s] feva_override.prompt_dag skipped: %r\n" % (_os.getpid(), _exc)
        try:
            with open("/tmp/feva_boot.log", "a", encoding="utf-8") as _f:
                _f.write("%s %s" % (_time.strftime("%H:%M:%S"), _msg))
        except Exception:
            pass
        _sys.stderr.write(_msg)
