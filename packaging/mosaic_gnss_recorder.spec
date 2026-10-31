# PyInstaller の設定。build.ps1 から使う。
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

analysis = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL"],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="mosaic_gnss_recorder",
    console=False,
)
coll = COLLECT(exe, analysis.binaries, analysis.datas, name="mosaic_gnss_recorder")
