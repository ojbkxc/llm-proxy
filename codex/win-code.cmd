@echo off
rem ============================================================
rem  win-code.cmd - Windows Codex 启动器（本地 / 远程服务器）
rem
rem  双击 = 菜单选择
rem  命令行带参 = 直接透传，例:
rem    win-code.cmd remote
rem    win-code.cmd remote --exec "只回复：收到"
rem    win-code.cmd local
rem    win-code.cmd local -p code      (kimi-k2.7-code 档)
rem    win-code.cmd local -p astra    (gpt-6-astra→glm-5.3 假名档)
rem    win-code.cmd list / health / config
rem
rem  模型档位(-p): deep=glm-5.3 / code=kimi-k2.7-code /
rem    dfast=deepseek-flash / fast=glm-flash / astra/sol/luna=GPT 假名
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

if "%~1"=="" goto menu
python win-code.py %*
exit /b %errorlevel%

:menu
echo.
echo  ============================================
echo   Codex 启动器 (win-code)
echo  ============================================
echo   1) 本地 Codex     (选模型档位)
echo   2) 远程服务器     (服务器会话, 交互 TUI)
echo   3) 列出远程线程
echo   4) 探活远程服务器
echo   5) 查看配置
echo   0) 退出
echo  ============================================
set "CHOICE="
set /p CHOICE=  请选择 [0-5]:

if "%CHOICE%"=="1" python win-code.py local
if "%CHOICE%"=="2" python win-code.py remote
if "%CHOICE%"=="3" python win-code.py list
if "%CHOICE%"=="4" python win-code.py health
if "%CHOICE%"=="5" python win-code.py config
if "%CHOICE%"=="0" exit /b 0
echo.
pause
exit /b 0