<#
.SYNOPSIS
    Run PyRunner in plugin DEV MODE locally (no Docker) as a full system:
    `runserver` with live reload + the django-q worker, in one command.

.DESCRIPTION
    The developer loop for building plugins - the counterpart to run-local.ps1
    (which is the PRODUCTION image: gunicorn + DEBUG=False, where dev mode is
    deliberately disabled). This script instead:

      * forces DEBUG=True              - the dev-mode triple-guard requires it;
      * sets PYRUNNER_PLUGIN_DEV        - your local plugin folder loads as
                                          plugins.<slug> via runserver's reloader
                                          child (RUN_MAIN). Edit a .py/template
                                          and refresh - no zip, upload, or restart;
      * starts `manage.py qcluster`     - the django-q worker, so "Run now" and
                                          scheduled runs ACTUALLY execute;
      * runs `manage.py runserver`      - in the foreground (web + worker logs
                                          stream together).

    Uses the local SQLite DB (data\db.sqlite3) - NOT the Docker Postgres stack.
    These DEBUG=True / PYRUNNER_PLUGIN_DEV values are set on THIS process only
    (load_dotenv won't override them), so your .env on disk is left untouched.
    Ctrl+C stops both the server and the worker.

.PARAMETER PluginPath
    Path to the plugin folder to dev-load (the folder name IS the slug).
    Default: examples\qdrant_backup.

.PARAMETER Port
    Host port for runserver. Default 8000.

.PARAMETER NoWorker
    Web only - don't start the django-q worker (runs will queue but not execute).

.PARAMETER NoMigrate
    Skip the migrate step on startup.

.EXAMPLE
    .\run-plugin-dev.ps1
        Dev-load examples\qdrant_backup with web + worker on http://127.0.0.1:8000.

.EXAMPLE
    .\run-plugin-dev.ps1 -PluginPath plugins\my_flows -Port 8010
        Dev-load a different plugin on a different port.
#>
[CmdletBinding()]
param(
    [string]$PluginPath = "examples\qdrant_backup",
    [int]$Port = 8000,
    [switch]$NoWorker,
    [switch]$NoMigrate
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Write-Step($m)  { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok($m)    { Write-Host "    $m" -ForegroundColor Green }
function Write-Warn2($m) { Write-Host "    $m" -ForegroundColor Yellow }

# --- Resolve the venv python ------------------------------------------------
$Python = Join-Path $Root 'venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw "venv python not found at $Python. Create the virtualenv first (python -m venv venv; pip install -r requirements.txt)."
}

# --- Resolve the plugin folder (absolute; folder name = slug) ---------------
$resolved = Resolve-Path -LiteralPath $PluginPath -ErrorAction SilentlyContinue
if (-not $resolved) { throw "Plugin folder not found: $PluginPath" }
$PluginFull = $resolved.Path
$Slug = Split-Path $PluginFull -Leaf

# --- Dev-mode environment (this process only) -------------------------------
# load_dotenv(override=False) keeps these, so a DEBUG=False in .env is overridden
# here without rewriting the file.
$env:DEBUG = "True"
$env:PYRUNNER_PLUGIN_DEV = $PluginFull
$env:PORT = "$Port"

Write-Step "PyRunner plugin DEV mode (full system: web + worker)"
Write-Host "    plugin:  $PluginFull  (slug: $Slug)"
Write-Host "    url:     http://127.0.0.1:$Port/plugins/$Slug/"
Write-Host "    db:      data\db.sqlite3  (SQLite - NOT the Docker Postgres stack)"
Write-Host "    web:     manage.py runserver (DEBUG=True, live reload)"
if (-not $NoWorker) { Write-Host "    worker:  manage.py qcluster (django-q - runs execute)" }
Write-Host ""

# --- Quick static-lint of the plugin (non-fatal in dev) ---------------------
Write-Step "Validating plugin (doctor)"
& $Python manage.py plugin_doctor --path $PluginFull
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "Doctor reported a BLOCKING issue (above) - it would be refused at activation. Continuing for dev."
}

# --- Migrate the dev DB (idempotent) ----------------------------------------
if (-not $NoMigrate) {
    Write-Step "Applying migrations (SQLite dev DB)"
    & $Python manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw "migrate failed (exit $LASTEXITCODE)." }
}

# --- Start the worker (background) + runserver (foreground) ------------------
$worker = $null
try {
    if (-not $NoWorker) {
        Write-Step "Starting django-q worker"
        # -NoNewWindow: shares this console so logs stream inline AND a console
        # Ctrl+C reaches it too; the finally below is the cleanup safety net.
        $worker = Start-Process -FilePath $Python -ArgumentList 'manage.py', 'qcluster' `
            -PassThru -NoNewWindow
        Write-Ok "Worker started (pid $($worker.Id))."
    }

    Write-Step "Starting runserver  ->  http://127.0.0.1:$Port/plugins/$Slug/"
    Write-Warn2 "Ctrl+C stops both the server and the worker."
    Write-Host ""
    & $Python manage.py runserver "127.0.0.1:$Port"
}
finally {
    if ($worker -and -not $worker.HasExited) {
        Write-Step "Stopping worker (pid $($worker.Id))"
        # qcluster spawns child workers - kill the whole tree.
        & taskkill /PID $worker.Id /T /F *> $null
    }
}
