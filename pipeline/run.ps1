# RetailPulse Detection Pipeline Runner (Windows / PowerShell)
#
# Usage:
#   .\pipeline\run.ps1                                   # Process all clips in clips_config.json
#   .\pipeline\run.ps1 --clip "Resources/CCTV Footage/CAM 1.mp4" --camera-type entry
#   .\pipeline\run.ps1 --frame-skip 5 --device cpu       # Slower frames, CPU only
#
# Output: data/events.jsonl

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

# Pick the venv interpreter if present, else fall back to python on PATH
$Python = "python"
if (Test-Path ".venv\Scripts\python.exe") {
    $Python = ".venv\Scripts\python.exe"
} elseif (Test-Path "venv\Scripts\python.exe") {
    $Python = "venv\Scripts\python.exe"
}

Write-Host "============================================================"
Write-Host " RetailPulse Detection Pipeline"
Write-Host " Project root: $ProjectRoot"
Write-Host " Output: data/events.jsonl"
Write-Host "============================================================"

& $Python -m pipeline.run `
    --clips-config data/clips_config.json `
    --layout data/store_layout.json `
    --output data/events.jsonl `
    --pos-csv data/pos_transactions.csv `
    @args

Write-Host "Done. Events written to data/events.jsonl"
