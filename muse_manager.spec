# PyInstaller — Muse Manager (Windows)
# Chạy: build.bat

block_cipher = None

hiddenimports = [
    "app_paths",
    "warehouse_store",
    "cookie_store",
    "muse_automation",
    "lay2fa_client",
    "mailtm_client",
    "flask",
    "werkzeug",
    "jinja2",
    "markupsafe",
    "itsdangerous",
    "click",
    "blinker",
    "waitress",
    "requests",
    "urllib3",
    "certifi",
    "charset_normalizer",
    "idna",
    "playwright",
    "playwright.sync_api",
    "playwright._impl",
    "greenlet",
    "pyee",
]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[("templates", "templates")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MuseManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
