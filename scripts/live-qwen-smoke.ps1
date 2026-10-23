param(
    [string]$Model,
    [string]$BaseUrl
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$modelArguments = @()
if ($Model) { $modelArguments += @('--model', $Model) }
if ($BaseUrl) { $modelArguments += @('--base-url', $BaseUrl) }
Push-Location -LiteralPath $taskRoot
try {
    conda run --no-capture-output -n coding-agent python -X utf8 -m examples.live_llm_smoke @modelArguments
    $smokeExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $smokeExitCode
