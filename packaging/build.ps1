# Builds mosaic_gnss_recorder.exe with PyInstaller, then the installer with Inno Setup.
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"

# Engines (malib_rt.exe, clas_rt.exe, CLASLIB, data) are built once; see engines\build_engines.ps1.
if (-not (Test-Path "$here\..\engines\bin\malib_rt.exe") -or
    -not (Test-Path "$here\..\engines\bin\clas_rt.exe")) {
    & "$here\..\engines\build_engines.ps1"
}

python -m PyInstaller --noconfirm --distpath "$here\dist" --workpath "$here\build" "$here\mosaic_gnss_recorder.spec"
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

& $iscc "$here\mosaic_gnss_recorder.iss"
if ($LASTEXITCODE -ne 0) { throw 'ISCC failed' }
