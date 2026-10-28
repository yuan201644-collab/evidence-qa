@echo off
REM ============================================================
REM  循证问答 · 直连 KAG 启动器
REM
REM  与 start.cmd 的区别：
REM    start.cmd  是老项目入口，会先检查 PostgreSQL 容器、加载本地模型，
REM               走的是项目自己的 RAG 编排链；
REM    本文件     只起「后端 + 前端」，且后端固定走 ASK_BACKEND=kag，
REM               把问题直接发给 KAG 服务。
REM
REM  KAG 地址不写在本文件里：从环境变量 KAG_BASE 或同目录 .env.local 读取。
REM  理由：这是公开仓库，内网实验服务器的地址不该进版本历史；
REM        而每个演示环境本来就要填自己的地址。
REM  详见 演示启动说明.md 的「准备 .env.local」一节。
REM ============================================================
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"

REM ---- 读 KAG 地址：环境变量优先，其次 .env.local ----
if not defined KAG_BASE if exist "%ROOT%.env.local" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%ROOT%.env.local") do (
    if /i "%%A"=="KAG_BASE" set "KAG_BASE=%%B"
  )
)
if not defined KAG_BASE goto :nobase

set "ASK_BACKEND=kag"
set "KAG_APP_ID=5"
set "KAG_TIMEOUT=420"

set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/2] 后端  http://127.0.0.1:8000   (ASK_BACKEND=kag → %KAG_BASE%)
start "循证问答 · 后端" cmd /k "cd /d %ROOT% && set ""ASK_BACKEND=kag"" && set ""KAG_BASE=%KAG_BASE%"" && set ""KAG_APP_ID=%KAG_APP_ID%"" && set ""KAG_TIMEOUT=%KAG_TIMEOUT%"" && "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

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
exit /b 0

:nobase
echo.
echo 未找到 KAG 服务地址，启动中止。
echo.
echo 请在本目录新建 .env.local，写入一行（把地址换成你的 KAG 主机）：
echo     KAG_BASE=http://你的KAG主机:8887
echo.
echo 该文件已被 .gitignore 忽略，不会进版本库。
echo 也可以先 set KAG_BASE=... 再运行本脚本。
echo.
pause
endlocal
exit /b 1
