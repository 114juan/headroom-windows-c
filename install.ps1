# headroom installer for Windows — installs launchers and auto-installs graphifyy.

# 1. Verify Python 3.9+
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Error "headroom needs python (3.9+) on PATH"
    exit 1
}

$pyVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$major, $minor = $pyVersion.Split('.')
if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 9)) {
    Write-Error "headroom needs Python 3.9+, found Python $pyVersion"
    exit 1
}

$REPO = $PSScriptRoot
if (-not $REPO) {
    $REPO = (Get-Location).Path
}

# 2. Install graphifyy
Write-Host "Installing/updating graphifyy (codebase mapping utility)..."
$installed = $false
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "Using uv to install graphifyy..."
    & uv pip install --upgrade graphifyy
    if ($LASTEXITCODE -eq 0) { $installed = $true }
}

if (-not $installed) {
    Write-Host "Using pip to install graphifyy..."
    & python -m pip install --upgrade graphifyy
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not install graphifyy automatically. Please run: pip install graphifyy"
    }
}

# 3. Create target bin directory
$TargetDir = $env:HEADROOM_BIN_DIR
if (-not $TargetDir) {
    $TargetDir = Join-Path $Home ".local\bin"
}
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

# 4. Generate Windows launchers (CMD and PowerShell)
$CmdPath = Join-Path $TargetDir "headroom.cmd"
$PsPath = Join-Path $TargetDir "headroom.ps1"

$CmdContent = @"
@echo off
setlocal
set HEADROOM_REPO=$REPO
python -c "import sys, os; sys.path.insert(0, os.environ['HEADROOM_REPO']); from headroom.__main__ import main; sys.exit(main())" %*
"@

$PsContent = @"
`$REPO = "$REPO"
`$env:HEADROOM_REPO = `$REPO
`$passed = @()
foreach (`$arg in `$args) {
    if (`$arg -eq "") {
        `$passed += '""'
    } else {
        `$passed += `$arg
    }
}
python -c "import sys, os; sys.path.insert(0, os.environ['HEADROOM_REPO']); from headroom.__main__ import main; sys.exit(main())" `$passed
"@

$CmdContent | Out-File -FilePath $CmdPath -Encoding ascii -Force
$PsContent | Out-File -FilePath $PsPath -Encoding utf8 -Force

Write-Host "Installed launchers in: $TargetDir"
Write-Host "  headroom.cmd -> $CmdPath"
Write-Host "  headroom.ps1 -> $PsPath"

# 5. Check PATH
$pathEnv = [Environment]::GetEnvironmentVariable("Path", "User")
$pathList = $pathEnv -split ";"
$alreadyOnPath = $false
foreach ($p in $pathList) {
    if (-not [string]::IsNullOrWhiteSpace($p)) {
        $resolvedP = (Resolve-Path $p -ErrorAction SilentlyContinue).Path
        $resolvedTarget = (Resolve-Path $TargetDir -ErrorAction SilentlyContinue).Path
        if ($resolvedP -and $resolvedTarget -and $resolvedP -eq $resolvedTarget) {
            $alreadyOnPath = $true
            break
        }
    }
}

if (-not $alreadyOnPath) {
    Write-Host ""
    Write-Warning "$TargetDir is not on your User PATH."
    Write-Host "To add it, run this in PowerShell:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path', 'User') + ';$TargetDir', 'User')"
    Write-Host "Then restart your terminal."
}

Write-Host ""
Write-Host "next: headroom setup" -ForegroundColor Green
