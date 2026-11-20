$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        & py -3 -m venv .venv
    } else {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $PythonCommand) {
            throw 'Python 3.10 or newer is required. Install Python and try again.'
        }
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}

& $Python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'local-finance-tracker requires Python 3.10 or newer. Recreate .venv with a supported Python version.' }

& $Python -c 'from importlib.metadata import version; from pathlib import Path; import sys; reqs=[line.strip().split("==", 1) for line in Path("requirements.txt").read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]; sys.exit(0 if all(version(name) == wanted for name, wanted in reqs) else 1)' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install Python dependencies.' }
}
& $Python src/setup.py
if ($LASTEXITCODE -ne 0) { throw 'local-finance-tracker setup did not complete.' }

Start-Process 'http://127.0.0.1:8765'
& $Python src/web.py
exit $LASTEXITCODE
