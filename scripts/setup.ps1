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
        Write-Host "    and every job costs money." -ForegroundColor Yellow
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
    Say "Checking Anthropic credential (rungs 1-5)"
    if ($env:ANTHROPIC_API_KEY) {
        Write-Host "    ANTHROPIC_API_KEY is set - rungs 1-5 use the raw API (cheap)."
    }
    else {
        Write-Host "    No ANTHROPIC_API_KEY." -ForegroundColor Yellow
        Write-Host "    Rungs 1-5 will fall back to the 'claude -p' CLI, which carries" -ForegroundColor Yellow
        Write-Host "    ~25-35k tokens of harness overhead per call. That is roughly" -ForegroundColor Yellow
        Write-Host "    50x the cost of the same task over the API." -ForegroundColor Yellow
        Write-Host "    To fix:  setx ANTHROPIC_API_KEY sk-ant-..." -ForegroundColor Yellow
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
        & claude mcp add ladder --scope user -- python $mcpPath 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "    Registered as 'ladder' at user scope."
        }
        else {
            Write-Host "    Already registered, or registration declined." -ForegroundColor Yellow
        }
    }

    Write-Host ""
    Say "Done." "Green"
    Write-Host "  Dashboard:  python -m ladder.server"
    Write-Host "              http://127.0.0.1:5151"
    Write-Host "  Benchmark:  python scripts/bench.py"
    Write-Host "  From Claude Code, the ladder_* tools are now available."
}
finally {
    Pop-Location
}
