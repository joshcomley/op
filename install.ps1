# op — install script
#
# Run from C:\D\op\ (the repo root). Idempotent: safe to re-run after
# `git pull` to pick up new code.
#
# Steps:
#   1. pip install -e . (installs op_gateway + op_cli + runtime deps)
#   2. Seed op.json from op.json.example if it doesn't exist
#   3. Run `op promote` to generate op.snapshot.json
#   4. Print the JSON snippet for registering op in ~/.claude.json
#
# Does NOT mutate ~/.claude.json — the user wires that up themselves
# (see INSTALL.md for the snippet) so they can choose machine-wide vs
# per-project registration without surprise.

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding           = [System.Text.Encoding]::UTF8

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

Write-Host "[op install] repo root: $RepoRoot"
Write-Host ""

# ---- Step 1: install package + dependencies -------------------------------
# Editable install puts op_gateway + op_cli on sys.path globally so
# `python -m op_gateway.server` works from any cwd. Required for MCP
# clients (Claude Code, in particular) that don't honor the `cwd` field
# in the stdio server config.
Write-Host "[op install] installing op-gateway (editable) + dependencies..."
& python -m pip install -e . --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install -e . failed"
    exit 1
}

# ---- Step 2: seed op.json -------------------------------------------------
$LiveJsonPath = Join-Path $RepoRoot "op.json"
$ExamplePath  = Join-Path $RepoRoot "op.json.example"

if (-not (Test-Path $LiveJsonPath)) {
    Write-Host "[op install] seeding op.json from op.json.example..."
    Copy-Item $ExamplePath $LiveJsonPath
} else {
    Write-Host "[op install] op.json already exists; not overwriting."
}

# ---- Step 3: promote ------------------------------------------------------
Write-Host "[op install] running op promote..."
& python -m op_cli promote
if ($LASTEXITCODE -ne 0) {
    Write-Error "op promote failed"
    exit 1
}

# ---- Step 4: registration hint --------------------------------------------
Write-Host ""
Write-Host "==========================================================="
Write-Host "[op install] OK."
Write-Host ""
Write-Host "Next step: register op in ~/.claude.json (or per-project .mcp.json)."
Write-Host "Add this entry under `"mcpServers`":"
Write-Host ""
Write-Host '  "op": {'
Write-Host '    "command": "python",'
Write-Host "    `"args`": [`"-m`", `"op_gateway.server`"]"
Write-Host '  }'
Write-Host ""
Write-Host "(No `"cwd`" needed — pip install -e put op_gateway on sys.path."
Write-Host " Some MCP clients ignore the cwd field anyway.)"
Write-Host ""
Write-Host "See INSTALL.md for the full snippet and rationale."
Write-Host "==========================================================="
