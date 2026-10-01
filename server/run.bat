@echo off
rem ===========================================================================
rem  MChat 服务端 - Windows 启动脚本（双击即用）
rem
rem  * 默认监听 0.0.0.0:8765，局域网里的同事就能连过来；
rem    只想本机用，把下面的 --host 0.0.0.0 改成 127.0.0.1。
rem  * 数据（数据库 + 附件）落在本脚本旁边的 data\ 目录里，
rem    备份就备这个目录；想换地方改 --data 后面的路径。
rem  * 第一次运行 Windows 防火墙会弹窗，选「允许访问」，否则别人连不上。
rem  * 关窗口 / 按 Ctrl+C 就是停服，服务端会先把数据落盘再退出。
rem  * 参数含义见同目录 README.md 的「参数表」。
rem ===========================================================================

chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo [mchat] 正在启动 MChat 服务端 ...
echo [mchat] 数据目录: %~dp0data
echo [mchat] 本机地址: http://127.0.0.1:8765
echo.

rem 优先用仓库自带的 venv（开发机上跑），没有就用系统 PATH 里的 python
set "PYEXE="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"

if defined PYEXE (
    "%PYEXE%" mchat_server.py --host 0.0.0.0 --port 8765 --data "%~dp0data"
) else (
    python mchat_server.py --host 0.0.0.0 --port 8765 --data "%~dp0data"
)

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo [mchat] 服务已正常退出。
) else (
    echo [mchat] 服务退出，退出码 %RC%
    echo [mchat] 端口被占就换个 --port；缺 python 就装一个 Python 3.9+ 并勾选 Add to PATH。
)
echo.
pause
