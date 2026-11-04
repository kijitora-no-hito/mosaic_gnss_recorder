# PyInstaller の設定。build.ps1 から使う。
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
# 測位エンジン(engines/build_engines.ps1 で作る)。bin・data・licenses が無ければ止める
ENGINE_DIRS = ("bin", "data", "templates", "licenses")
for name in ENGINE_DIRS:
    if not os.path.isdir(os.path.join(ROOT, "engines", name)):
        raise SystemExit(f"engines/{name} がありません。engines/build_engines.ps1 を先に実行してください。")

analysis = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL"],
    datas=[(os.path.join(ROOT, "engines", name), os.path.join("engines", name))
           for name in ENGINE_DIRS],
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
