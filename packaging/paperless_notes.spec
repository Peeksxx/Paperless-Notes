# PyInstaller spec for the Paperless Notes onedir build. Run it through tools\build_release.ps1.
# Windowed, no UPX, version resource and icon from the repository, and only the Qt parts the app uses:
# the collected file list is filtered by tools/release.py so network, QML, web, multimedia, SVG and PDF
# modules, translations and unused plugins never ship.

import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from paperless_notes.branding import PRODUCT_NAME  # noqa: E402
from tools.release import BUILD_EXCLUDES, allowed_file, version_resource  # noqa: E402

analysis = Analysis(
    [str(ROOT / "src" / "paperless_notes" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(BUILD_EXCLUDES),
    noarchive=False,
    optimize=0,
)
analysis.binaries = [entry for entry in analysis.binaries if allowed_file(entry[0])]
analysis.datas = [entry for entry in analysis.datas if allowed_file(entry[0])]
pyz = PYZ(analysis.pure)

version_file = Path(workpath) / "version_info.txt"
version_file.parent.mkdir(parents=True, exist_ok=True)
version_file.write_text(version_resource(), encoding="utf-8")

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=PRODUCT_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "paperless-notes.ico"),
    version=str(version_file),
)
coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=PRODUCT_NAME,
)
