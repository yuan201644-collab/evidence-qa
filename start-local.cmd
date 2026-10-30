@echo off
chcp 65001 >nul
REM ============================================================
REM  循证问答 · 本机演示（评审用最小路径）
REM
REM  与 start.cmd / start-kag.cmd 的区别：
REM    start.cmd     老项目入口：拉 PostgreSQL 容器 + 加载 6.5 GB 本地模型
REM    start-kag.cmd 需要已有 KAG 服务地址（.env.local 里的 KAG_BASE）
REM    本文件        不需要 KAG、不加载模型，只跑「界面 + 冻结题库 + 离线事实表」
REM
REM  首次使用前：python -m pip install -r requirements-local.txt
REM  详见 docs\评审复现指南.md
REM ============================================================
setlocal
set "ROOT=%~dp0"

where python >nul 2>nul
if errorlevel 1 goto :nopython

python "%ROOT%scripts\start_local.py" %*
if errorlevel 1 (
  echo.
  echo 启动失败。若提示缺少依赖，请先执行：
  echo     python -m pip install -r requirements-local.txt
)
echo.
pause >nul
exit /b 0

:nopython
echo 未找到 python 命令。请先安装 Python 3.11 及以上，并勾选 "Add to PATH"。
echo.
pause >nul
exit /b 1
