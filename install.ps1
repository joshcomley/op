# op — install script
#
# Run from C:\D\op\ (the repo root). Idempotent: safe to re-run after
# `git pull` to pick up new code.
#
# Steps:
#   1. pip install -r requirements.txt (mcp, pydantic)
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

# ---- Step 1: dependencies -------------------------------------------------
Write-Host "[op install] installing Python dependencies..."
& python -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed"
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
$cwdEscaped = $RepoRoot -replace '\\', '\\'
Write-Host "    `"args`": [`"-m`", `"op_gateway.server`"],"
Write-Host "    `"cwd`":  `"$cwdEscaped`""
Write-Host '  }'
Write-Host ""
Write-Host "See INSTALL.md for the full snippet and rationale."
Write-Host "==========================================================="
