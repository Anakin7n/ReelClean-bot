@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

title ReelClean-bot 安装程序

echo.
echo ========================================
echo   ReelClean-bot 一键安装程序
echo ========================================
echo.

:: ============================================================
:: Step 1: 检查 Python
:: ============================================================
echo [1/5] 检查 Python 环境...
echo.

set "PYTHON="

for /f "tokens=*" %%i in ('where python3 2^>nul') do (
    set "PYTHON=%%i"
    goto :found_python
)

for /f "tokens=*" %%i in ('where python 2^>nul') do (
    set "PYTHON=%%i"
    goto :found_python
)

:found_python
if "!PYTHON!"=="" (
    echo   [错误] 未找到 Python，请先安装 Python 3.12+
    echo   下载地址: https://www.python.org/downloads/
    echo   安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo   找到: !PYTHON!

:: 获取 Python 版本号
"!PYTHON!" -c "import sys; print(sys.version.split()[0])" > "%TEMP%\py_ver_temp.txt" 2>&1
set /p PY_VER=<"%TEMP%\py_ver_temp.txt"
del "%TEMP%\py_ver_temp.txt" 2>nul
echo   Python 版本: !PY_VER!

:: 检查版本是否 >= 3.12
for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)

if "!PY_MAJOR!"=="" (
    echo   [错误] 无法检测 Python 版本
    pause
    exit /b 1
)

if !PY_MAJOR! LSS 3 (
    echo   [错误] Python 版本过低，需要 3.12+
    pause
    exit /b 1
)
if !PY_MAJOR! EQU 3 (
    if !PY_MINOR! LSS 12 (
        echo   [错误] Python 版本过低，当前 !PY_VER!，需要 3.12+
        pause
        exit /b 1
    )
)
echo   [通过] Python 版本符合要求
echo.

:: ============================================================
:: Step 2: 创建虚拟环境
:: ============================================================
echo [2/5] 创建虚拟环境 .venv ...
echo.

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    echo   虚拟环境已存在，跳过创建。
) else (
    if exist ".venv" (
        echo   旧的 .venv 目录存在但不完整，正在删除重建...
        rmdir /s /q ".venv"
    )
    "!PYTHON!" -m venv .venv
    if errorlevel 1 (
        echo   [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
    echo   [完成] 虚拟环境已创建
)
echo.

:: ============================================================
:: Step 3: 升级 pip
:: ============================================================
echo [3/5] 升级 pip ...
echo.

.\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo   [警告] pip 升级失败，继续安装依赖...
) else (
    echo   [完成] pip 已升级
)
echo.

:: ============================================================
:: Step 4: 安装依赖
:: ============================================================
echo [4/5] 安装项目依赖 ...
echo.

if not exist "requirements.txt" (
    echo   [错误] 未找到 requirements.txt 文件
    pause
    exit /b 1
)

.\.venv\Scripts\pip.exe install -r requirements.txt
if errorlevel 1 (
    echo   [错误] 依赖安装失败，请检查网络连接后重试
    pause
    exit /b 1
)
echo   [完成] 所有 Python 依赖已安装
echo.

:: ============================================================
:: Step 5: 检查 .env 配置
:: ============================================================
echo [5/5] 检查飞书凭证配置 ...
echo.

if not exist ".env" (
    echo   [注意] 未找到 .env 文件，正在创建模板...
    (
        echo FEISHU_APP_ID=cli_xxxxxxxx
        echo FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxx
    ) > ".env"
    echo   [重要] .env 模板已创建，请用记事本打开 .env 文件
    echo          填入你的飞书应用凭证后保存：
    echo.
    echo          FEISHU_APP_ID=cli_xxxxxxxx
    echo          FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxx
    echo.
    echo   是否现在打开 .env 文件编辑？[Y/N]
    set /p "OPEN_ENV="
    if /i "!OPEN_ENV!"=="Y" start notepad ".env"
) else (
    echo   .env 文件已存在
)
echo.

:: ============================================================
:: 安装完成
:: ============================================================
echo ========================================
echo   安装完成！
echo ========================================
echo.
echo   启动方式:
echo     1. 双击 start.vbs（推荐，零闪屏）
echo     2. 右键 start.ps1 - 使用 PowerShell 运行
echo.
echo   使用流程:
echo     1. 在飞书群聊发送 3 个 Excel 文件
echo     2. 发送参数文本，格式如下：
echo        目标排片:0.2 总成本:300000 后台消耗:32.8 上一时段:83.4 D8百分比:4.4
echo     3. 机器人自动回复 3 份文案和 2 个处理后的文件
echo.

pause
exit /b 0
