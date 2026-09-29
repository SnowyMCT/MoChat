# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onefile + 无控制台（等价于 `-F -w`）+ 全部本地素材。

    python tools/build_exe.py          # 推荐：打包 + 自检素材真的进去了
    pyinstaller main.spec              # 等价，手动跑

**别删 datas**：`datas=[('mchat/assets', 'mchat/assets')]` 是本地素材的唯一来源 ——
旗帜精灵图 + 逐张 PNG（`assets/demo/` 里那两张附件素材现在只给测试用，一并带上无妨）。
少了它：冻结后 `flags.sprite_exists()/png_exists()` 全假，界面整体降级成纯文本 ——
Windows 的 Segoe UI Emoji 不画国旗，于是旗帜全变成 "CN" "JP" 两个字母。

（同理：别再用 `pyinstaller -F -w main.py` 直接打包 —— 那条命令不带任何 data 文件，
PyInstaller 还会顺手把本文件重写成 `datas=[]` 的默认版。）
"""


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('mchat/assets', 'mchat/assets')],      # ← 旗帜素材：精灵图 + 逐张 PNG
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='main',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,              # = 命令行里的 -w（不弹黑框）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
