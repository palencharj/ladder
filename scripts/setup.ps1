<#
.SYNOPSIS
    One-shot setup for Ladder on Windows.
.DESCRIPTION
    Installs Python dependencies, checks for Ollama, pulls the rung-0 model,
    and registers the MCP server with Claude Code. Safe to re-run.
.PARAMETER Model
    Local model for rung 0. Default qwen3-coder:30b.
.PARAMETER SkipModel
    Skip the model pull (it is an 18 GB download).
.EXAMPLE
    .\scripts\setup.ps1
.EXAMPLE
    .\scripts\setup.ps1 -Model qwen2.5-coder:7b
#>
[CmdletBinding()]
param(
    [string]$Model = "qwen3-coder:30b",
    [switch]$SkipModel
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

function Say([string]$msg, [string]$colour = "Cyan") {
    Write-Host "==> $msg" -ForegroundColor $colour
}

try {
    # ---- Python ----------------------------------------------------------
    Say "Checking Python"
    $pyVersion = & python --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Python not found on PATH. Install Python 3.10+ and re-run."
    }
    Write-Host "    $pyVersion"

    Say "Installing dependencies"
    & python -m pip install --quiet --upgrade pip
    & python -m pip install --quiet -e ".[dev,api]"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    Write-Host "    flask, anthropic, pytest, ruff installed"

    # ---- Ollama ----------------------------------------------------------
    Say "Checking Ollama (rung 0, the free tier)"
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Write-Host "    Ollama not found." -ForegroundColor Yellow
        Write-Host "    Install from https://ollama.com/download, then re-run." -ForegroundColor Yellow
        Write-Host "    Ladder still works without it, but rung 0 is unavailable" -ForegroundColor Yellow
        Write-Host "    and every request spends subscription allowance." -ForegroundColor Yellow
    }
    else {
        Write-Host "    $(& ollama --version 2>&1 | Select-Object -First 1)"

        if ($SkipModel) {
            Say "Skipping model pull (-SkipModel)"
        }
        else {
            $installed = (& ollama list 2>$null | Out-String)
            if ($installed -match [regex]::Escape($Model)) {
                Write-Host "    $Model already present"
            }
            else {
                Say "Pulling $Model (large download; this takes a while)"
                & ollama pull $Model
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "    Pull failed. Rung 0 will be unavailable." -ForegroundColor Yellow
                }
            }
        }
    }

    # ---- API credential --------------------------------------------------
    Say "Checking how rungs 1-5 will run"
    if ($env:ANTHROPIC_API_KEY) {
        Write-Host "    ANTHROPIC_API_KEY is set - rungs 1-5 use the raw API."
    }
    else {
        Write-Host "    No ANTHROPIC_API_KEY: rungs 1-5 will use 'claude -p'"
        Write-Host "    against your Claude Code subscription. That is the normal"
        Write-Host "    setup for a prepaid plan - there is no bill to worry about."
        Write-Host ""
        Write-Host "    What to watch instead is allowance. Each 'claude -p' call"
        Write-Host "    spends ~35k tokens of harness overhead before touching your"
        Write-Host "    task, charged per invocation however small the work. Two"
        Write-Host "    levers keep that down:"
        Write-Host "      - let mechanical work run at rung 0, where it costs nothing"
        Write-Host "      - pass batch=true to ladder_swarm for bulk paid work"
        Write-Host "    Run ladder_report to see how well both are working."
    }

    # ---- Verify ----------------------------------------------------------
    Say "Running tests"
    & python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }

    Say "Verifying MCP server"
    & python scripts/check_mcp.py
    if ($LASTEXITCODE -ne 0) { throw "MCP handshake failed" }

    # ---- Register with Claude Code ---------------------------------------
    Say "Registering the MCP server with Claude Code"
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if (-not $claude) {
        Write-Host "    claude CLI not found; skipping registration." -ForegroundColor Yellow
        Write-Host "    A .mcp.json is checked in, so Claude Code will pick it up" -ForegroundColor Yellow
        Write-Host "    automatically when run from this directory." -ForegroundColor Yellow
    }
    else {
        $mcpPath = Join-Path $root "mcp\ladder_mcp.py"
        # PowerShell 5.1 turns a native command's stderr into ErrorRecords, so
        # a benign "already exists" prints as a red NativeCommandError and looks
        # like a crash. Capture the output as plain text instead.
        $addOutput = & cmd /c "claude mcp add ladder --scope user -- python `"$mcpPath`" 2>&1"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    Registered as 'ladder' at user scope."
        }
        elseif ($addOutput -match "already exists") {
            Write-Host "    Already registered at user scope - nothing to do."
        }
        else {
            Write-Host "    Could not register automatically:" -ForegroundColor Yellow
            Write-Host "      $addOutput" -ForegroundColor Yellow
            Write-Host "    A .mcp.json is checked in, so Claude Code picks it up" -ForegroundColor Yellow
            Write-Host "    when run from this directory regardless." -ForegroundColor Yellow
        }
    }

    Write-Host ""
    Say "Done." "Green"
    Write-Host "  Dashboard:  python -m ladder.server"
    Write-Host "              http://127.0.0.1:5151"
    Write-Host "  Benchmark:  python scripts/bench.py   (know your local speed)"
    Write-Host ""
    Write-Host "  RESTART Claude Code to pick up the ladder_* tools." -ForegroundColor Cyan
    Write-Host "  Then try:  'Use ladder_health to check the engines'"
    Write-Host "             'Use ladder_report to see if this is worth running'"
}
finally {
    Pop-Location
}
