@echo off
REM Build the myCodingAgent VS Code extension into a .vsix installer
REM Requirements: Node.js (npm) installed

cd /d "%~dp0"

where npx >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js / npm not found. Install from https://nodejs.org
    pause
    exit /b 1
)

echo Packaging extension...
call npx @vscode/vsce package --allow-missing-repository
if errorlevel 1 (
    echo [ERROR] Packaging failed.
    pause
    exit /b 1
)

echo.
echo Done. Install with:
for %%f in (*.vsix) do echo   code --install-extension "%%f"
pause
