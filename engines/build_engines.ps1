# Builds malib_rt.exe and clas_rt.exe, and collects the CLASLIB executable, data files and licenses
# into engines\bin, engines\data and engines\licenses (used by packaging\build.ps1).
#
# Requires:
#   MALIB   (https://github.com/JAXA-SNU/MALIB)                clone, -Malib
#   CLASLIB (https://github.com/QZSS-Strategy-Office/claslib)  clone, -Claslib
#   MSYS2 ucrt64 gcc (pacman -S mingw-w64-ucrt-x86_64-gcc),    -Msys
param(
    [string]$Malib = 'C:\work\60_tools\MALIB',
    [string]$Claslib = 'C:\work\60_tools\claslib',
    [string]$Msys = 'C:\msys64'
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$bin = Join-Path $here 'bin'
$data = Join-Path $here 'data'
$licenses = Join-Path $here 'licenses'
foreach ($dir in $bin, $data, $licenses, (Join-Path $data 'pubkey')) {
    New-Item -ItemType Directory -Force $dir | Out-Null
}

# gcc and ld write temporary files to %TMP%; it must be writable.
$tmp = Join-Path $env:LOCALAPPDATA 'Temp\mosaic_engines_build'
New-Item -ItemType Directory -Force $tmp | Out-Null
$env:TMP = $tmp
$env:TEMP = $tmp
$env:PATH = "$Msys\ucrt64\bin;$env:PATH"

$src = Join-Path $Malib 'src'
$names = 'rtksvr', 'biassnx', 'ephemeris', 'geoid', 'inscmn', 'ionex', 'lambda', 'lclcmn',
         'mdccssr', 'mdciono', 'options', 'pntpos', 'ppp', 'ppp_ar', 'preceph', 'rcvraw',
         'qzssl6', 'readfcb', 'rinex', 'rtcm', 'rtcm2', 'rtcm3', 'rtcm3lcl', 'rtcm3e', 'rtkcmn',
         'rtkpos', 'sbas', 'solution', 'stream', 'tides', 'satqual'
$sources = @($names | ForEach-Object { Join-Path $src "$_.c" })
# malib_rt\septentrio.c replaces MALIB's SBF decoder (L6 frame selection and gap handling).
$sources += @(Get-ChildItem (Join-Path $src 'rcv\*.c') | Where-Object Name -ne 'septentrio.c' |
              ForEach-Object FullName)
$sources += Join-Path $here 'malib_rt\septentrio.c'
$sources += Join-Path $here 'malib_rt\malib_rt.c'
# WIN32 selects the Windows thread/socket code in rtklib.h; MinGW does not define it.
& gcc -pipe -O2 -std=gnu99 -mcmodel=medium -DWIN32 -DENAGLO -DENAGAL -DENAQZS -DENACMP `
    -DENAIRN -DNFREQ=10 -DNEXOBS=5 '-DPROGNAME="malib_rt"' -I $src `
    -o (Join-Path $bin 'malib_rt.exe') $sources -lws2_32 -lwinmm
if ($LASTEXITCODE -ne 0) { throw 'malib_rt build failed' }

# clas_rt: CLASLIB (same sources and options as util\rnx2rtkp\makefile) + seclib (mbedTLS).
# clas_rt.c includes postpos.c itself, so postpos.c is not in the list.
$csrc = Join-Path $Claslib 'src'
$sec = Join-Path $Claslib 'seclib'
$secobj = Join-Path $tmp 'seclib'
New-Item -ItemType Directory -Force $secobj | Out-Null
$secsources = @(Get-ChildItem (Join-Path $sec 'third_party\mbedtls\library\*.c'))
$secsources += @(Get-ChildItem (Join-Path $sec 'src\*.c'))
foreach ($file in $secsources) {
    & gcc -pipe -O2 -c -I (Join-Path $sec 'include') `
        -I (Join-Path $sec 'third_party\mbedtls\include') `
        -I (Join-Path $sec 'third_party\mbedtls\library') `
        -o (Join-Path $secobj "$($file.BaseName).o") $file.FullName
    if ($LASTEXITCODE -ne 0) { throw "seclib build failed: $($file.Name)" }
}
$cnames = 'rtkcmn', 'rinex', 'rtkpos', 'solution', 'lambda', 'geoid', 'sbas', 'preceph',
          'pntpos', 'ephemeris', 'options', 'ppp', 'ppp_ar', 'rtcm', 'rtcm2', 'rtcm3', 'rtcm3e',
          'ionex', 'qzslex', 'rcvraw', 'stec', 'isb', 'grid', 'cssr_l6e', 'clas_pubkey',
          'cssr_decode', 'cssr_bank', 'cssr_misc', 'cssr_output', 'cssr_cntinuty', 'ppprtk',
          'cssr2osr', 'rtkvrs'
$csources = @($cnames | ForEach-Object { Join-Path $csrc "$_.c" })
$csources += @('binex', 'novatel', 'ublox' | ForEach-Object { Join-Path $csrc "rcv\$_.c" })
$csources += Join-Path $here 'clas_rt\clas_rt.c'
$csources += @(Get-ChildItem (Join-Path $secobj '*.o') | ForEach-Object FullName)
& gcc -pipe -O2 -std=gnu99 -DWIN32 -DTRACE -DENAGAL -DENAQZS -DNFREQ=3 -DENA_PPP_RTK `
    -DENA_REL_VRS -DENA_CSSR_AUTH -I $csrc -I (Join-Path $sec 'include') `
    -o (Join-Path $bin 'clas_rt.exe') $csources -lws2_32 -lwinmm -lbcrypt
if ($LASTEXITCODE -ne 0) { throw 'clas_rt build failed' }

Copy-Item (Join-Path $Claslib 'util\rnx2rtkp\rnx2rtkp.exe') (Join-Path $bin 'clas_rnx2rtkp.exe')
Copy-Item (Join-Path $Malib 'data\igs20_2423.atx') $data
foreach ($name in 'clas_grid.def', 'clas_grid.blq', 'igu00p01.erp', 'igs14_L5copy.atx',
                  'isb.tbl', 'l2csft.tbl') {
    Copy-Item (Join-Path $Claslib "data\$name") $data
}
Copy-Item (Join-Path $Claslib 'data\pubkey\*') (Join-Path $data 'pubkey')

Copy-Item (Join-Path $Malib 'LICENSE.txt') (Join-Path $licenses 'MALIB_LICENSE.txt')
Copy-Item (Join-Path $Claslib 'LICENSE') (Join-Path $licenses 'CLASLIB_LICENSE.txt')
Copy-Item (Join-Path $Claslib 'readme_rtklib.txt') (Join-Path $licenses 'CLASLIB_readme_rtklib.txt')
# The CLASLIB LICENSE refers to its README for the RTKLIB / GSILIB / mbedTLS components.
Copy-Item (Join-Path $Claslib 'README.md') (Join-Path $licenses 'CLASLIB_README.md')
Get-ChildItem (Join-Path $Claslib 'seclib\LICENSES') -File | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $licenses "CLASLIB_seclib_$($_.Name).txt")
}
Write-Host "engines ready: $bin"
