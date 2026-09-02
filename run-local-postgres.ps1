<#
.SYNOPSIS
    Run the full PyRunner stack locally in Docker ON POSTGRES, as it runs in a
    Postgres production deployment.

.DESCRIPTION
    The Postgres counterpart to run-local.ps1. Builds the same production image
    and runs it via docker-compose.postgres.yml - the app container plus a
    managed Postgres service. The app container is the whole app stack:

      * gunicorn  -> WSGI web server (NOT manage.py runserver)
      * django-q2 -> background task worker (started by entrypoint.sh, with the
                     same auto-restart monitor used in production)
      * WhiteNoise serves the collected/compressed static files; DEBUG is OFF.

    The ONLY difference from run-local.ps1 is the database: instead of the
    zero-config SQLite file, this points the app at a Postgres service via
    DATABASE_URL (built from the POSTGRES_* values in .env). It exercises the
    exact engine-agnostic path a Postgres deployment uses.

    On start it ensures a .env with real SECRET_KEY / ENCRYPTION_KEY, pins the
    production-parity flags (DEBUG=False, SECURE_SSL_REDIRECT=False), and
    provisions stable POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB (the
    password is generated once and preserved, so it keeps matching the persistent
    Postgres data volume across runs). Postgres data lives in the named volume
    `pyrunner_pgdata`; venvs/workdir live in `pyrunner_data`. -Fresh wipes both.

    Note: this stack and the SQLite run-local.ps1 stack both use the container
    name `pyrunner`, so run only one at a time (use the other script's -Down to
    switch). Requires Docker Desktop running.

.PARAMETER Port
    Host port to publish the app on. Default 8124 (the container always listens
    on 8000 internally; this only changes the host side of the mapping).

.PARAMETER Dev
    Plugin DEV MODE on this same stack (same Postgres data/volumes): layers
    docker-compose.dev.yml so the app container boots `manage.py runserver`
    (live reload; dev mode's RUN_MAIN guard) with DEBUG=True instead of
    gunicorn, and bind-mounts the plugin folder from the repo. Plugin edits
    reload live - no rebuild, no zip, no reinstall. Note: if the same slug is
    installed AND ACTIVE on the instance, deactivate it first (dev mode won't
    shadow it). Core code still runs from the image (rebuild to pick it up).

.PARAMETER Plugin
    With -Dev: the plugin slug to develop. The folder is auto-located under
    .\plugins\<slug> (private) or .\examples\<slug> (public). Default: gmail_agent.

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
    Stop and remove the stack (keeps the data + Postgres volumes), then exit.

.PARAMETER Fresh
    Tear down AND delete the data + Postgres volumes (fresh DB / first-run
    setup), then do a clean rebuild and start.

.EXAMPLE
    .\run-local-postgres.ps1
        Build the production image and run the full stack on Postgres, streaming
        logs here. Ctrl+C stops it.

.EXAMPLE
    .\run-local-postgres.ps1 -Detached
        Same, but run in the background. Use -Logs to watch, -Down to stop.

.EXAMPLE
    .\run-local-postgres.ps1 -Fresh
        Wipe the data + Postgres volumes and start completely clean.

.EXAMPLE
    .\run-local-postgres.ps1 -Dev -Detached
        Same stack + data, but in plugin dev mode (default plugin: gmail_agent).

.EXAMPLE
    .\run-local-postgres.ps1 -Dev -Plugin youtube_scout
        Dev-load a different plugin (found in .\plugins\ or .\examples\).
#>
[CmdletBinding()]
param(
    [int]$Port = 8124,
    [switch]$NoBuild,
    [switch]$Rebuild,
    [switch]$Detached,
    [switch]$Logs,
    [switch]$Down,
    [switch]$Fresh,
    [switch]$Sandbox,
    [switch]$Dev,
    [string]$Plugin = 'gmail_agent'
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
Set-Location $Root

function Write-Step($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

$ComposeFile = Join-Path $Root 'docker-compose.postgres.yml'
# Opt-in (-Sandbox): relax the container's seccomp/AppArmor so unprivileged user
# namespaces work and the FULL script sandbox (bwrap/nsjail) can be tested. Local
# only — see docker-compose.sandbox-test.yml. Off by default (locked-down parity).
$SandboxOverride = Join-Path $Root 'docker-compose.sandbox-test.yml'
# Opt-in (-Dev): plugin dev mode on the same stack - runserver + DEBUG + the
# plugin folder bind-mounted (see docker-compose.dev.yml).
$DevOverride = Join-Path $Root 'docker-compose.dev.yml'

# --- Dev mode: resolve the plugin folder (name IS the slug) ------------------
if ($Dev) {
    $devDir = $null
    foreach ($home_ in @('plugins', 'examples')) {
        if (Test-Path (Join-Path $Root "$home_\$Plugin\apps.py")) { $devDir = $home_; break }
    }
    if (-not $devDir) {
        throw "Plugin '$Plugin' not found (no plugins\$Plugin\apps.py or examples\$Plugin\apps.py)."
    }
    # Consumed by docker-compose.dev.yml's mount + PYRUNNER_PLUGIN_DEV.
    $env:DEV_PLUGIN = $Plugin
    $env:DEV_PLUGIN_DIR = "./$devDir"
}

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
    $all = @('-f', $ComposeFile)
    if ($Sandbox) { $all += @('-f', $SandboxOverride) }   # relaxed-seccomp test layer
    if ($Dev)     { $all += @('-f', $DevOverride) }       # plugin dev-mode layer
    $all += $Rest
    if ($script:UseV2) { & docker compose @all } else { & docker-compose @all }
}

# --- 2. Lifecycle short-circuits (down / fresh / logs) -----------------------
if ($Down) {
    Write-Step "Stopping PyRunner (keeping data + Postgres volumes)"
    Compose down
    Write-Ok "Stopped. Volumes 'pyrunner_data' and 'pyrunner_pgdata' preserved."
    return
}

if ($Logs) {
    Write-Step "Following logs (Ctrl+C to stop watching)"
    Compose logs -f
    return
}

if ($Fresh) {
    Write-Step "Tearing down stack AND deleting data + Postgres volumes"
    Compose down -v --remove-orphans
    # `down -v` only removes volumes it can attribute to THIS compose project.
    # A stack brought up under another project name, or a half-finished earlier
    # teardown, leaves them behind and the "fresh" boot silently reuses old
    # data (QA sweep 2026-07-23, F1). Remove the three named volumes explicitly
    # and verify, so -Fresh either wipes or says loudly that it could not.
    $project = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { (Split-Path -Leaf $PSScriptRoot).ToLower() }
    foreach ($suffix in @('pyrunner_data', 'pyrunner_plugins', 'pyrunner_pgdata')) {
        $name = "${project}_$suffix"
        $exists = docker volume ls -q --filter "name=^${name}$"
        if ($exists) {
            Write-Warn2 "Volume '$name' survived 'down -v' - removing it explicitly."
            docker volume rm -f $name | Out-Null
        }
    }
    $left = @(docker volume ls -q --filter "name=pyrunner_pgdata") + @(docker volume ls -q --filter "name=pyrunner_data")
    $left = $left | Where-Object { $_ }
    if ($left) {
        throw "-Fresh could not remove volume(s): $($left -join ', '). Remove them by hand (docker volume rm) and retry."
    }
    Write-Ok "Clean slate - Postgres DB, venvs and environments removed (verified: no pyrunner volumes left)."
}

# --- 3. Ensure .env with production-parity + Postgres values -----------------
# Generated entirely in PowerShell so this works without a local Python/venv.
function New-UrlSafeToken([int]$bytes) {
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    # base64url, no padding -> safe inside a .env value AND inside a DATABASE_URL
    # (no '#', '$', '/', '+', '@', ':').
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

# Postgres credentials. The password is generated once and preserved, so it
# keeps matching the persistent `pyrunner_pgdata` volume on later runs (changing
# it without -Fresh would lock you out of the existing DB).
Set-IfMissing 'POSTGRES_USER'     'pyrunner'
Set-IfMissing 'POSTGRES_PASSWORD' (New-UrlSafeToken 24)
Set-IfMissing 'POSTGRES_DB'       'pyrunner'

# Force the production-parity flags (this is the whole point of this script).
# In -Dev the compose override injects DEBUG=True into the container directly,
# so .env is left alone - a later parity run restores it.
if (-not $Dev) {
    if ($envVars['DEBUG'] -ne 'False') {
        Write-Warn2 "Setting DEBUG=False for production parity (was '$($envVars['DEBUG'])')."
    }
    $envVars['DEBUG'] = 'False'
}
$envVars['SECURE_SSL_REDIRECT'] = 'False'   # plain http locally; edge/proxy does TLS in prod
# Secure cookies require HTTPS; on plain-http localhost a Secure cookie is never
# sent back, which silently logs you out on every request. Off for local runs.
$envVars['SESSION_COOKIE_SECURE'] = 'False'
$envVars['CSRF_COOKIE_SECURE'] = 'False'

# Write back only if something changed, without a UTF-8 BOM (a BOM corrupts the
# first key for Docker Compose's .env parser).
$lines = @('# Maintained by run-local-postgres.ps1 - PyRunner Postgres-parity local run (Docker).',
    '# SECRET_KEY / ENCRYPTION_KEY / POSTGRES_PASSWORD are preserved across runs; keep them.')
foreach ($k in $envVars.Keys) { $lines += "$k=$($envVars[$k])" }
$content = ($lines -join "`n") + "`n"

if (($envVars | Out-String) -ne $before -or -not (Test-Path $envFile)) {
    [System.IO.File]::WriteAllText($envFile, $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok ".env updated for Postgres-parity run."
} else {
    Write-Ok ".env already Postgres-ready."
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

if ($Sandbox) {
    Write-Warn2 "SANDBOX TEST MODE: seccomp relaxed (docker-compose.sandbox-test.yml)."
    Write-Warn2 "  This lowers the container's own isolation so unprivileged user namespaces"
    Write-Warn2 "  work and the FULL script sandbox can be tested. Do NOT use this in production."
}

if ($Dev) {
    Write-Step "Starting PyRunner stack on Postgres in PLUGIN DEV MODE -> $url"
    Write-Host "    plugin:  $($env:DEV_PLUGIN_DIR)/$($env:DEV_PLUGIN)  (live reload -> $url/plugins/$($env:DEV_PLUGIN)/)"
    Write-Host "    web:     manage.py runserver (DEBUG=True, autoreload)"
    Write-Host "    worker:  django-q2 pyrunner_qcluster (respawn loop)"
    Write-Host "    db:      postgres:16-alpine - SAME data/volumes as the parity stack"
    Write-Warn2 "If '$($env:DEV_PLUGIN)' is installed AND ACTIVE here, deactivate it (Plugins page) or dev mode won't load."
} else {
    Write-Step "Starting full PyRunner stack on Postgres (gunicorn + django-q2 + db) -> $url"
    Write-Host "    image:   built from Dockerfile (production)"
    Write-Host "    web:     gunicorn pyrunner.wsgi (DEBUG=False, WhiteNoise static)"
    Write-Host "    worker:  django-q2 qcluster (auto-restart monitor)"
    Write-Host "    db:      postgres:16-alpine (DATABASE_URL -> db:5432)"
    Write-Host "    data:    volumes 'pyrunner_pgdata' (DB) + 'pyrunner_data' (venvs)"
}
Write-Host ""

if ($Detached) {
    Compose @upArgs
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed (exit $LASTEXITCODE)." }
    Write-Host ""
    Write-Ok "PyRunner (Postgres) is starting in the background at $url"
    Write-Warn2 "First start waits for Postgres, then runs migrations + setup; give it a few seconds."
    Write-Host ""
    Write-Host "    Follow logs:  .\run-local-postgres.ps1 -Logs"   -ForegroundColor Yellow
    Write-Host "    Stop:         .\run-local-postgres.ps1 -Down"   -ForegroundColor Yellow
    Write-Host "    Fresh start:  .\run-local-postgres.ps1 -Fresh"  -ForegroundColor Yellow
} else {
    Write-Host "  PyRunner (Postgres) will be available at $url" -ForegroundColor Green
    Write-Host "  Press Ctrl+C to stop the stack." -ForegroundColor Yellow
    Write-Host ""
    # Foreground: compose streams logs and handles Ctrl+C (graceful stop).
    Compose @upArgs
}
