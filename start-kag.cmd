@echo off
REM ============================================================
REM  循证问答 · 直连 KAG 启动器
REM
REM  与 start.cmd 的区别：
REM    start.cmd  是老项目入口，会先检查 PostgreSQL 容器、加载本地模型，
REM               走的是项目自己的 RAG 编排链；
REM    本文件     只起「后端 + 前端」，且后端固定走 ASK_BACKEND=kag，
REM               把问题直接发给实验服务器上的 KAG。
REM
REM  前提：能访问 10.166.33.23:8887（校园网内网，无需 SSH 隧道）。
REM ============================================================
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"
set "ASK_BACKEND=kag"
set "KAG_BASE=http://10.166.33.23:8887"
set "KAG_APP_ID=5"
set "KAG_TIMEOUT=420"

set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/2] 后端  http://127.0.0.1:8000   (ASK_BACKEND=kag)
start "循证问答 · 后端" cmd /k "cd /d %ROOT% && set ""ASK_BACKEND=kag"" && set ""KAG_BASE=http://10.166.33.23:8887"" && set ""KAG_APP_ID=5"" && set ""KAG_TIMEOUT=420"" && "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo [2/2] 前端  http://127.0.0.1:5184
start "循证问答 · 前端" cmd /k "cd /d %ROOT%web && npm run dev"

echo.
echo 等待服务起来（约 8 秒）后会自动打开浏览器。
echo 演示账号：admin / Aic2026Demo!
echo 单题耗时 1-5 分钟属正常（KAG 会先规划再检索再生成）。
echo.
timeout /t 8 >nul
start "" http://127.0.0.1:5184

endlocal
