param([switch]$Start)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
}
& .venv/Scripts/python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if ($Start) {
    if ($env:NAVIQRA_SECRET.Length -lt 32) { throw 'Set a persistent NAVIQRA_SECRET before starting.' }
    & .venv/Scripts/waitress-serve.exe --listen=127.0.0.1:8080 --call station:create_app
    if ($LASTEXITCODE -ne 0) { throw 'Station service stopped with an error.' }
}
