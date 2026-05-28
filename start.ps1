Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (Test-Path ".\.venv\Scripts\python.exe") {
    $python = ".\.venv\Scripts\python.exe"
    Write-Host "[OK] Using venv Python"
} else {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        Write-Host "[ERROR] Python not found. Run install.bat first."
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "[OK] Using system Python: $python"
}

& $python auto_bot.py