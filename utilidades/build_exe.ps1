# Atajo interno. El generador que se usa es ..\build.bat
$ErrorActionPreference = "Stop"
$bat = Join-Path (Split-Path $PSScriptRoot -Parent) "build.bat"
& cmd /c "`"$bat`" nopause"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
