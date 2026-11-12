param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..")
try {
    New-Item -ItemType Directory -Force logs/evaluation | Out-Null
    & $Python -m unittest discover -s tests -v 2>&1 | Tee-Object logs/evaluation/tests.log
    if ($LASTEXITCODE -ne 0) { throw "Evaluation tests failed" }
    & $Python -m evaluation.audit
    if ($LASTEXITCODE -ne 0) { throw "Positive risk audit failed" }
    & $Python -m evaluation.audit --strategy-root evaluation/reference_original --output results/evaluation/original-risk-audit.json 2>&1 | Tee-Object logs/evaluation/negative-control.log
    if ($LASTEXITCODE -eq 0) { throw "Unsafe original unexpectedly passed the audit" }
    $oldPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = (Resolve-Path evaluation/reference).Path
        & $Python -m optiver_bot.demo
        if ($LASTEXITCODE -ne 0) { throw "Offline demo failed" }
    } finally { $env:PYTHONPATH = $oldPythonPath }
    & $Python -m evaluation.compare generate --seed 1729 --frames 600
    if ($LASTEXITCODE -ne 0) { throw "Synthetic generation failed" }
    & $Python -m evaluation.compare run 2>&1 | Tee-Object logs/evaluation/comparison.log
    if ($LASTEXITCODE -ne 0) { throw "Comparison failed" }
} finally { Pop-Location }
