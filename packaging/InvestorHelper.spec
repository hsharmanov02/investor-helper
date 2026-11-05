# PyInstaller spec: builds a single windowed InvestorHelper.exe
# Run from the repo root:  pyinstaller packaging/InvestorHelper.spec
from PyInstaller.utils.hooks import collect_all

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
fp_datas, fp_binaries, fp_hidden = collect_all("feedparser")

a = Analysis(
    ["../run_investor_helper.py"],
    pathex=[".."],
    binaries=pw_binaries + fp_binaries,
    datas=pw_datas + fp_datas + [("../investor_helper/data", "investor_helper/data")],
    hiddenimports=pw_hidden + fp_hidden + ["zoneinfo", "tzdata"],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PySide6.QtWebEngineCore",
              "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="InvestorHelper",
    console=False,
    upx=False,
    icon=None,
)
