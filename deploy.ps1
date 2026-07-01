param(
    [string]$Remote = "my",
    [string]$Branch = "v5",
    [string]$Config = "deploy.remote",
    [string]$Target = "all",
    [switch]$SkipPush
)

$ErrorActionPreference = "Stop"

function Fail($Message) {
    Write-Error $Message
    exit 1
}

function Quote-Bash($Value) {
    return "'" + ($Value -replace "'", "'\''") + "'"
}

function Invoke-Checked($File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "Command failed with exit code ${LASTEXITCODE}: $File $($Arguments -join ' ')"
    }
}

if (-not (Test-Path ".git")) {
    Fail "Current directory is not a git repository. Run this script from the project root."
}

if (-not (Test-Path $Config)) {
    Fail "Missing $Config. Copy deploy.remote.example to $Config and fill in your server list."
}

$servers = @()
$lineNumber = 0
foreach ($line in Get-Content $Config) {
    $lineNumber += 1
    $trimmed = $line.Trim()
    if ($trimmed -eq "" -or $trimmed.StartsWith("#")) {
        continue
    }

    $parts = $trimmed -split "\|", 3
    if ($parts.Count -ne 3) {
        Fail "Invalid $Config line $lineNumber. Expected: name|ssh_user_host|remote_project_dir"
    }

    $servers += [pscustomobject]@{
        Name = $parts[0].Trim()
        Host = $parts[1].Trim()
        Dir = $parts[2].Trim()
    }
}

if ($servers.Count -eq 0) {
    Fail "No servers found in $Config."
}

if ($Target -ne "all") {
    $wanted = $Target.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
    $servers = $servers | Where-Object { $wanted -contains $_.Name }

    if ($servers.Count -eq 0) {
        Fail "No matching servers for target '$Target'."
    }
}

Invoke-Checked "git" @("rev-parse", "--verify", $Branch)

if (-not $SkipPush) {
    Write-Host "Pushing $Branch to ${Remote}:${Branch}..."
    Invoke-Checked "git" @("push", $Remote, "${Branch}:${Branch}")
}

foreach ($server in $servers) {
    Write-Host "Updating $($server.Name) on $($server.Host):$($server.Dir)..."

    $remoteDir = Quote-Bash $server.Dir
    $remoteName = Quote-Bash $Remote
    $branchName = Quote-Bash $Branch
    $remoteCommand = "set -e; cd $remoteDir; git fetch $remoteName $branchName; git checkout $branchName || git checkout -b $branchName FETCH_HEAD; git pull --ff-only $remoteName $branchName"

    Invoke-Checked "ssh" @($server.Host, $remoteCommand)
}

Write-Host "Deploy complete."
