<#
.SYNOPSIS
    Run the full PyRunner stack locally in Docker, exactly as it runs in production.

.DESCRIPTION
    Builds the production image from the Dockerfile and runs it via
    docker-compose.yml - the same definition used for self-hosting. That single
    container is the whole stack:

      * gunicorn  -> WSGI web server (NOT manage.py runserver)
      * django-q2 -> background task worker (started by entrypoint.sh, with the
                     same auto-restart monitor used in production)
      * WhiteNoise serves the collected/compressed static files (built into the
        image), so there is no Tailwind watcher and DEBUG is OFF.

    On start it ensures a .env with real SECRET_KEY / ENCRYPTION_KEY and pins the
    production-parity flags (DEBUG=False, SECURE_SSL_REDIRECT=False so plain http
    works locally). The SQLite DB and environments live in the named Docker
    volume `pyrunner_data`, so they persist across runs (use -Fresh to wipe).

    Requires Docker Desktop running. The first build is slow (it installs Node +
    the Claude CLI + Python deps); later runs reuse the layer cache.

.PARAMETER Port
    Host port to publish the app on. Default 8123 (the container always listens
    on 8000 internally; this only changes the host side of the mapping).

.PARAMETER NoBuild
    Start the existing image without rebuilding (fastest; skips picking up code
    changes). By default the image is (re)built so your latest code is included.

.PARAMETER Rebuild
    Force a clean, no-cache rebuild of the image before starting.

.PARAMETER Detached
    Run the stack in the background (-d) and return, instead of streaming logs.

.PARAMETER Logs
    Follow the logs of the already-running stack, then exit. (Does not start it.)

.PARAMETER Down
    Stop and remove the stack (keeps the data volume), then exit.

.PARAMETER Fresh
    Tear down AND delete the data volume (fresh DB / first-run setup), then do a
    clean rebuild and start.

.EXAMPLE
    .\run-local.ps1
        Build the production image and run the full stack, streaming logs here.
        Ctrl+C stops it.

.EXAMPLE
    .\run-local.ps1 -Detached
        Same, but run in the background. Use -Logs to watch, -Down to stop.

.EXAMPLE
    .\run-local.ps1 -Fresh
        Wipe the data volume and start completely clean.
#>
[CmdletBinding()]
param(
    [int]$Port = 8123,
    [switch]$NoBuild,
    [switch]$Rebuild,
    [switch]$Detached,
    [switch]$Logs,
    [switch]$Down,
    [switch]$Fresh
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Write-Step($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

$ComposeFile = Join-Path $Root 'docker-compose.yml'

# --- 1. Verify Docker + Compose ---------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker not found on PATH. Install Docker Desktop and try again."
}

# Is the daemon actually running?
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is installed but the daemon isn't reachable. Start Docker Desktop and retry."
}

# Prefer the v2 plugin (`docker compose`); fall back to legacy `docker-compose`.
& docker compose version *> $null
if ($LASTEXITCODE -eq 0) {
    $script:UseV2 = $true
} elseif (Get-Command docker-compose -ErrorAction SilentlyContinue) {
    $script:UseV2 = $false
} else {
    throw "Docker Compose not found (neither 'docker compose' nor 'docker-compose')."
}

function Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)
    $all = @('-f', $ComposeFile) + $Rest
    if ($script:UseV2) { & docker compose @all } else { & docker-compose @all }
}

# --- 2. Lifecycle short-circuits (down / fresh / logs) -----------------------
if ($Down) {
    Write-Step "Stopping PyRunner (keeping data volume)"
    Compose down
    Write-Ok "Stopped. Data volume 'pyrunner_data' preserved."
    return
}

if ($Logs) {
    Write-Step "Following logs (Ctrl+C to stop watching)"
    Compose logs -f
    return
}

if ($Fresh) {
    Write-Step "Tearing down stack AND deleting data volume"
    Compose down -v
    Write-Ok "Clean slate - DB and environments removed."
}

# --- 3. Ensure .env with production-parity values ----------------------------
# Generated entirely in PowerShell so this works without a local Python/venv.
function New-UrlSafeToken([int]$bytes) {
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    # base64url, no padding -> safe inside a .env value (no '#', '$', '/', '+').
    [Convert]::ToBase64String($buf).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}
function New-FernetKey {
    # Fernet key = urlsafe-base64 of 32 random bytes (44 chars incl. '=' padding).
    $buf = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    [Convert]::ToBase64String($buf).Replace('+', '-').Replace('/', '_')
}

$envFile = Join-Path $Root '.env'
$envVars = [ordered]@{}
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $envVars[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
    }
}

$before = ($envVars | Out-String)

function Set-IfMissing($key, $value) {
    if (-not $envVars.Contains($key) -or [string]::IsNullOrWhiteSpace($envVars[$key]) `
            -or $envVars[$key] -like 'your-*') {
        $envVars[$key] = $value
        Write-Ok "Generated $key"
    }
}

Set-IfMissing 'SECRET_KEY'     (New-UrlSafeToken 48)
Set-IfMissing 'ENCRYPTION_KEY' (New-FernetKey)
if (-not $envVars.Contains('ALLOWED_HOSTS')) { $envVars['ALLOWED_HOSTS'] = 'localhost,127.0.0.1' }

# Force the production-parity flags (this is the whole point of this script).
if ($envVars['DEBUG'] -ne 'False') {
    Write-Warn2 "Setting DEBUG=False for production parity (was '$($envVars['DEBUG'])')."
}
$envVars['DEBUG'] = 'False'
$envVars['SECURE_SSL_REDIRECT'] = 'False'   # plain http locally; edge/proxy does TLS in prod
# Secure cookies require HTTPS; on plain-http localhost a Secure cookie is never
# sent back, which silently logs you out on every request. Off for local runs.
$envVars['SESSION_COOKIE_SECURE'] = 'False'
$envVars['CSRF_COOKIE_SECURE'] = 'False'

# Write back only if something changed, without a UTF-8 BOM (a BOM corrupts the
# first key for Docker Compose's .env parser).
$lines = @('# Maintained by run-local.ps1 - PyRunner production-parity local run (Docker).',
    '# SECRET_KEY / ENCRYPTION_KEY are preserved across runs; keep them if you store secrets.')
foreach ($k in $envVars.Keys) { $lines += "$k=$($envVars[$k])" }
$content = ($lines -join "`n") + "`n"

if (($envVars | Out-String) -ne $before -or -not (Test-Path $envFile)) {
    [System.IO.File]::WriteAllText($envFile, $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok ".env updated for production-parity run."
} else {
    Write-Ok ".env already production-ready."
}

# --- 4. Build + run ----------------------------------------------------------
# Host port for the ${PORT:-8000}:8000 mapping. The container always binds 8000
# internally (compose pins PORT=8000 in the service env), so this only moves the
# host side - no conflict with the literal container port.
$env:PORT = "$Port"
$url = "http://localhost:$Port"

if ($Rebuild) {
    Write-Step "Rebuilding image from scratch (--no-cache)"
    Compose build --no-cache
    if ($LASTEXITCODE -ne 0) { throw "Image build failed (exit $LASTEXITCODE)." }
}

$upArgs = @('up')
if (-not $NoBuild -and -not $Rebuild) { $upArgs += '--build' }
if ($Detached) { $upArgs += '-d' }

Write-Step "Starting full PyRunner stack (gunicorn + django-q2 worker) -> $url"
Write-Host "    image:   built from Dockerfile (production)"
Write-Host "    web:     gunicorn pyrunner.wsgi (DEBUG=False, WhiteNoise static)"
Write-Host "    worker:  django-q2 qcluster (auto-restart monitor)"
Write-Host "    data:    docker volume 'pyrunner_data' (persists across runs)"
Write-Host ""

if ($Detached) {
    Compose @upArgs
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed (exit $LASTEXITCODE)." }
    Write-Host ""
    Write-Ok "PyRunner is starting in the background at $url"
    Write-Warn2 "First start runs migrations + setup; give it a few seconds."
    Write-Host ""
    Write-Host "    Follow logs:  .\run-local.ps1 -Logs"   -ForegroundColor Yellow
    Write-Host "    Stop:         .\run-local.ps1 -Down"   -ForegroundColor Yellow
    Write-Host "    Fresh start:  .\run-local.ps1 -Fresh"  -ForegroundColor Yellow
} else {
    Write-Host "  PyRunner will be available at $url" -ForegroundColor Green
    Write-Host "  Press Ctrl+C to stop the stack." -ForegroundColor Yellow
    Write-Host ""
    # Foreground: compose streams logs and handles Ctrl+C (graceful stop).
    Compose @upArgs
}
