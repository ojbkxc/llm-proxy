@echo off
rem ============================================================
rem deploy_ai_cli 一键部署/修复脚本（Windows）
rem 用途：在新电脑上一条命令完成 Claude / Codex 配置部署
rem       + Windows 沙箱问题自动修复（Temp ACL）
rem 要求：以管理员身份运行（右键 -> 以管理员身份运行）
rem ============================================================

setlocal enabledelayedexpansion

rem ── 定位脚本目录（支持中文/空格路径）──
pushd "%~dp0"
set "SCRIPT_DIR=%cd%"
set "PY_SCRIPT=%SCRIPT_DIR%\deploy_ai_cli.py"
popd

echo.
echo ============================================================
echo   deploy_ai_cli 一键部署（Claude + Codex 配置 + 沙箱修复）
echo ============================================================
echo   脚本目录: %SCRIPT_DIR%
echo.

rem ── 1. 管理员权限检查（--auto-fix 的 icacls 需要管理员）──
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 当前不是管理员权限！
    echo        Temp ACL 自动修复需要管理员权限。
    echo        请右键本脚本 -^> 「以管理员身份运行」。
    echo.
    choice /C YN /M "仍要继续（跳过自动修复）吗"
    if errorlevel 2 exit /b 1
    set "AUTO_FIX="
) else (
    echo [OK] 管理员权限确认
    set "AUTO_FIX=--auto-fix"
)

rem ── 2. Python 检查 ──
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 python，请先安装 Python 3.10+
    echo        https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version 2^>^&1') do set "PY_VER=%%i"
echo [OK] %PY_VER%
echo.

rem ── 3. Codex 检查（可选，缺失时提示安装）──
where codex >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 未检测到 codex。如需部署 Codex，先执行：
    echo        npm install -g @openai/codex@latest
    echo.
) else (
    for /f "tokens=*" %%i in ('codex --version 2^>^&1') do echo [OK] %%i
)

rem ── 4. 执行部署（默认信任 C:\GitHub 下两个常用项目目录）──
echo.
echo ------------------------------------------------------------
echo 开始部署...
echo ------------------------------------------------------------
rem 信任项目列表可在下方自行增删（每行一个 --trust-project）
python "%PY_SCRIPT%" --non-interactive %AUTO_FIX% ^
    --trust-project "C:\GitHub\AIGX" ^
    --trust-project "C:\GitHub\deploy_ai_cli"

if %errorlevel% neq 0 (
    echo.
    echo [失败] 部署出错（退出码 %errorlevel%），请查看上方日志。
    echo        可加 --verbose 重跑排查：python "%PY_SCRIPT%" --verbose
    pause
    exit /b 1
)

echo.
echo ------------------------------------------------------------
echo 部署完成！后续步骤：
echo ------------------------------------------------------------
echo   1. 重开终端（让环境变量 CF_GATEWAY_KEY / ANTHROPIC_* 生效）
echo   2. Codex 用法：
echo      - TUI 模式（推荐，可逐条审批命令）: codex
echo      - 自动化（无沙箱，信任任务用）:
echo            codex exec --sandbox danger-full-access "任务"
echo      - 切模型: codex --profile fast/mid/code/deep
echo   3. Claude 子代理: code-reviewer / fast-writer / architect
echo   4. 回滚本次改动: python "%PY_SCRIPT%" --rollback
echo.
pause
exit /b 0
