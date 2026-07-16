param(
    [string]$Remote = "my",
    [string]$Branch = "v5",
    [string]$Config = "deploy.remote",
    [string]$Target = "all",
    [switch]$Push
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

function Invoke-CheckedRetry($File, [string[]]$Arguments, [int]$Attempts = 3, [int]$DelaySeconds = 3) {
    for ($attempt = 1; $attempt -le $Attempts; $attempt += 1) {
        & $File @Arguments
        if ($LASTEXITCODE -eq 0) {
            return
        }
        if ($attempt -lt $Attempts) {
            Write-Warning "Command failed (attempt $attempt/$Attempts), retrying in $DelaySeconds seconds: $File"
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    Fail "Command failed after $Attempts attempts with exit code ${LASTEXITCODE}: $File $($Arguments -join ' ')"
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

$branchRef = "refs/heads/$Branch"
& "git" "show-ref" "--verify" "--quiet" $branchRef
if ($LASTEXITCODE -ne 0) {
    Fail "Local branch '$Branch' does not exist."
}

if ($Push) {
    Write-Host "Pushing $Branch to ${Remote}:${Branch}..."
    Invoke-Checked "git" @("push", $Remote, "${Branch}:${Branch}")
}

$bundleName = "dmr-deploy-$([guid]::NewGuid().ToString('N')).bundle"
$localBundle = Join-Path ([System.IO.Path]::GetTempPath()) $bundleName

try {
    Write-Host "Creating bundle for $branchRef..."
    Invoke-Checked "git" @("bundle", "create", $localBundle, $branchRef)

    foreach ($server in $servers) {
        Write-Host "Updating $($server.Name) on $($server.Host):$($server.Dir)..."

        # A unique /tmp name is safe to pass to scp and avoids relying on any
        # network access from the application server.
        $remoteBundle = "/tmp/$bundleName"
        $remoteDir = Quote-Bash $server.Dir
        $quotedBundle = Quote-Bash $remoteBundle
        $quotedBranchRef = Quote-Bash $branchRef

        try {
            Invoke-CheckedRetry "scp" @($localBundle, "$($server.Host):$remoteBundle")

            # Fetch and verify the uploaded bundle before touching the working tree.
            # If either command fails, reset/clean are not reached and the current
            # server checkout remains intact.
            $remoteCommand = "set -e; cd $remoteDir; git fetch $quotedBundle $quotedBranchRef; git reset --hard FETCH_HEAD; git clean -fd"
            Invoke-CheckedRetry "ssh" @($server.Host, $remoteCommand)
        }
        finally {
            # Best-effort cleanup also covers an interrupted scp or failed fetch.
            $cleanupSucceeded = $false
            for ($attempt = 1; $attempt -le 3; $attempt += 1) {
                & "ssh" $server.Host "rm -f $quotedBundle"
                if ($LASTEXITCODE -eq 0) {
                    $cleanupSucceeded = $true
                    break
                }
                if ($attempt -lt 3) {
                    Start-Sleep -Seconds 2
                }
            }
            if (-not $cleanupSucceeded) {
                Write-Warning "Could not remove temporary bundle on $($server.Name): $remoteBundle"
            }
        }
    }
}
finally {
    if (Test-Path -LiteralPath $localBundle) {
        Remove-Item -LiteralPath $localBundle -Force
    }
}

Write-Host "Deploy complete."
