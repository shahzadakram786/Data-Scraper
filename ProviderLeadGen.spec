# -*- mode: python ; coding: utf-8 -*-
#
# Build:  pyinstaller ProviderLeadGen.spec
#
# hiddenimports matters here. The app imports its engines lazily, inside the
# methods that need them, so a proxy or credentials change can take effect
# without restarting — which means PyInstaller's static analysis does not see
# them and would ship an exe that dies on the first click. Anything imported
# lazily has to be listed.


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Project modules imported lazily at run time
        'npi_engine',
        'email_engine',
        'psychology_today_engine',
        'lead_manager',
        'storage',
        'pipeline',
        'verification',
        'cms_engine',
        'outreach',
        'http_pool',
        'ui_theme',
        'ui_help',
        'updater',
        'version',
        'health',
        'errors',
        'config',
        'catalog',
        'cli',
        # Third-party packages that are imported inside functions
        'keyring',
        'keyring.backends.Windows',
        'dns',
        'dns.resolver',
        'gspread',
        'oauth2client.service_account',
        'matplotlib',
        'matplotlib.backends.backend_tkagg',
        'openpyxl',
        'ddgs',
        'lxml',
        'lxml._elementpath',
        'bs4',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Test-only; nothing in the shipped app imports it.
        'pytest',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ProviderLeadGen',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
