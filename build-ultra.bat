@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   comic-download-ultra 打包工具
echo ============================================

echo [1/3] 安装依赖（首次需联网）...
python -m pip install --upgrade pip
python -m pip install requests pillow websocket-client pyinstaller

echo [2/3] 打包 exe...
python -m PyInstaller --noconfirm --clean --onefile --console --name comic-download-ultra ^
  --collect-all websocket ^
  --collect-all requests ^
  --collect-all PIL ^
  comic-download-ultra.py

if errorlevel 1 (
  echo.
  echo 打包失败，请把上面的报错发给开发者。
  pause
  exit /b 1
)

echo [3/3] 复制到桌面...
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%D"
copy /y "dist\comic-download-ultra.exe" "%DESKTOP%\comic-download-ultra.exe" >nul
if errorlevel 1 (
  echo 复制失败，可手动把 dist\comic-download-ultra.exe 拖到桌面。
  pause
  exit /b 1
)

echo.
echo ============================================
echo   完成！桌面已生成 comic-download-ultra.exe
echo ============================================
pause
