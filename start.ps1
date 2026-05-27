Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (Test-Path ".\.venv\Scripts\python.exe") {
    $python = ".\.venv\Scripts\python.exe"
    Write-Host "[启动] 使用虚拟环境 Python"
} else {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        Write-Host "[错误] 未找到 Python，请先运行 install.bat 或安装 Python 3.12+"
        Read-Host "按回车退出"
        exit 1
    }
    Write-Host "[启动] 使用系统 Python: $python"
}

& $python auto_bot.py
