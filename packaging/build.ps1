# Builds mosaic_gnss_recorder.exe with PyInstaller, then the installer with Inno Setup.
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"

python -m PyInstaller --noconfirm --distpath "$here\dist" --workpath "$here\build" "$here\mosaic_gnss_recorder.spec"
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

& $iscc "$here\mosaic_gnss_recorder.iss"
if ($LASTEXITCODE -ne 0) { throw 'ISCC failed' }
