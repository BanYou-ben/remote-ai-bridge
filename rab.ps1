param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RabArguments
)

$candidates = @()
if ($env:RAB_PYTHON) {
    $candidates += ,@($env:RAB_PYTHON)
}
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    $candidates += ,@($pyLauncher.Source, "-3.10")
}
foreach ($name in @("python3.13", "python3.12", "python3.11", "python3.10", "python")) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        $candidates += ,@($command.Source)
    }
}

foreach ($candidate in $candidates) {
    $executable = $candidate[0]
    $prefixArguments = @($candidate | Select-Object -Skip 1)
    try {
        $version = & $executable @prefixArguments -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -eq "1") {
            & $executable @prefixArguments -m app.cli @RabArguments
            exit $LASTEXITCODE
        }
    }
    catch {
        continue
    }
}

Write-Error "Python 3.10 or newer was not found. Install it or set RAB_PYTHON to its executable path."
exit 2
