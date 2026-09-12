@echo off
REM ============================================================
REM  推送到 GitHub 私有仓库  ME-liang/TradeAI
REM  用法：双击本文件，或在项目目录的终端里运行它
REM  首次运行会由 Git Credential Manager 弹出浏览器，
REM  登录 GitHub 账号并点 "Authorize" 后自动继续推送。
REM ============================================================

REM 若本机 push 报 "CRYPT_E_REVOCATION_OFFLINE"（证书吊销检查失败），
REM 取消下面这行的注释即可绕过（该问题在部分 Windows 环境会出现）：
REM git config --global http.schannelCheckRevoke false

cd /d %~dp0
echo 当前目录: %CD%
echo.
echo 正在推送 main 分支到 origin (首次会弹出浏览器授权 GitHub) ...
echo.
git push -u origin main

echo.
echo 推送结束，退出码: %ERRORLEVEL%
echo (退出码 0 = 成功)
pause
