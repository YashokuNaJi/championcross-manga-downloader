@echo off
chcp 65001 >nul
setlocal

set "PORT=9222"
set "PROFILE=%USERPROFILE%\.cc_dl_profile"

set "EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%EXE%" set "EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%EXE%" set "EXE=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if not exist "%EXE%" (
    echo 没有找到 Chrome，请先安装，或改用 start-edge.bat。
    pause
    exit /b 1
)

echo 正在以调试模式启动 Chrome（端口 %PORT%）...
start "" "%EXE%" --remote-debugging-port=%PORT% --remote-allow-origins=* --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check "https://championcross.jp/"

echo.
echo 已启动。请在这个浏览器里登录 championcross，
echo 登录好后回到工具窗口，按提示继续操作。
pause
