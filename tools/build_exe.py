"""一条命令打出"带旗帜素材"的 exe，并当场验证素材真进去了。

用法：
    python tools/build_exe.py            # 打包（onefile + 无控制台）+ 自检
    python tools/build_exe.py --clean    # 先删 build/ 缓存再打

为什么不用 `pyinstaller -F -w main.py`
-------------------------------------
那条命令不带任何 data 文件，而旗帜是**图片素材**（mchat/assets/flags.png + 逐张 PNG）：
冻结后 flags.sprite_exists()/png_exists() 全假，界面整体降级成纯文本，
Windows 的 Segoe UI Emoji 不画国旗 → 旗帜全部变成 "CN" "JP" 两个字母。
所以素材必须按 datas 打进去（见 main.spec），并且**打包后当场核对**：
直接读 PyInstaller 自己写的 build/main/PKG-00.toc 清单，确认 mchat/assets 下的文件都在包里。

素材缺失、或清单对不上时脚本以非 0 退出，别把这种 exe 发出去。

用哪个解释器打包 = 冻结哪个 PyQt6：脚本优先用当前解释器里的 PyInstaller，
没有才退回 PATH 上的 `pyinstaller` 命令（会把当次实际用的解释器打出来）。
"""
from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "main.spec")
ASSETS = os.path.join(ROOT, "mchat", "assets")
SPRITE = os.path.join(ASSETS, "flags.png")
PNG_DIR = os.path.join(ASSETS, "flags")
TOC_PATH = os.path.join(ROOT, "build", "main", "PKG-00.toc")
TOC_PREFIX = "mchat" + os.sep + "assets"          # 清单里的条目名（Windows 上是反斜杠）


def fail(message: str) -> None:
    print(f"  [!] {message}")
    raise SystemExit(1)


# --------------------------------------------------------------------- 素材
def check_assets() -> int:
    """打包前先确认素材齐：缺素材打出来的 exe 一定是坏的，现在报比事后翻包快。"""
    sys.path.insert(0, ROOT)
    from mchat.ui import flags as flag_data               # 零 Qt 依赖，纯查表

    if not os.path.isfile(SPRITE):
        fail(f"缺精灵图：{SPRITE}（跑 tools/build_flags.py 生成）")
    if not os.path.isdir(PNG_DIR):
        fail(f"缺逐张 PNG 目录：{PNG_DIR}（跑 tools/build_flags.py 生成）")
    names = [n for n in os.listdir(PNG_DIR) if n.endswith(".png")]
    if len(names) != len(flag_data.FLAGS):
        fail(f"逐张 PNG 数量不对：{len(names)} != {len(flag_data.FLAGS)}"
             f"（跑 tools/build_flags.py 重新生成）")
    expected = len(names) + 1                             # +1 = 精灵图
    print(f"  OK  素材就位：flags.png + {len(names)} 张 PNG，共 {expected} 个文件")
    return expected


# ---------------------------------------------------------------- PyInstaller
def find_pyinstaller() -> list[str]:
    """优先用当前解释器里的 PyInstaller，其次 PATH 上的 pyinstaller 命令。"""
    for command in ([sys.executable, "-m", "PyInstaller"], ["pyinstaller"]):
        try:
            done = subprocess.run([*command, "--version"], capture_output=True,
                                  text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout.strip():
            print(f"  OK  {' '.join(command)}  →  PyInstaller {done.stdout.strip()}")
            return command
    fail("没找到 PyInstaller：pip install pyinstaller，或用装了它的解释器跑本脚本")


def build(command: list[str], *, clean: bool) -> None:
    work = os.path.join(ROOT, "build")
    if clean and os.path.isdir(work):
        shutil.rmtree(work)
        print("  已清 build/ 缓存")
    target = os.path.join(ROOT, "dist", "main.exe")
    if os.path.isfile(target):                    # PyInstaller 覆盖前必须先删掉
        try:
            os.remove(target)
        except OSError:
            # 正开着旧 exe 时删不掉（Windows 允许改名、不允许删除运行中的映像），
            # 那就把它挪到 main.exe.old —— 旧进程继续用那份文件，我们照常出新 exe。
            backup = target + ".old"
            try:
                if os.path.isfile(backup):
                    os.remove(backup)
                os.replace(target, backup)
                print(f"  dist/main.exe 正被占用：旧文件挪到 {os.path.basename(backup)}"
                      f"（关掉那个窗口后删掉它就行）")
            except OSError:
                fail(f"{target} 被占用：先关掉正在运行的 main.exe 再打包")
    args = [*command, SPEC, "--noconfirm", "--distpath", "dist", "--workpath", "build"]
    print(f"  运行：{' '.join(args)}")
    done = subprocess.run(args, cwd=ROOT)
    if done.returncode != 0:
        fail(f"PyInstaller 失败（退出码 {done.returncode}）")


# ------------------------------------------------------------------- 自检
def verify_bundle(expected: int) -> None:
    """读 PyInstaller 自己写的清单，确认素材真进了包（否则这次打包就是白打）。"""
    if not os.path.isfile(TOC_PATH):
        fail(f"找不到打包清单：{TOC_PATH}")
    with open(TOC_PATH, encoding="utf-8", errors="replace") as fh:
        entries = ast.literal_eval(fh.read())[2]
    bundled = [str(item[0]) for item in entries if str(item[0]).startswith(TOC_PREFIX)]
    if not any(name.endswith("flags.png") for name in bundled):
        fail(f"精灵图没进包 —— {TOC_PREFIX} 下只找到 {len(bundled)} 个文件，"
             f"检查 main.spec 的 datas 是否被删")
    if len(bundled) < expected:
        fail(f"素材不全：包里 {len(bundled)} 个 < 该有的 {expected} 个")
    print(f"  OK  包里素材核对通过：{len(bundled)} 个文件（含 flags.png）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 MChat 为单文件 exe（带旗帜素材）")
    parser.add_argument("--clean", action="store_true", help="先删 build/ 缓存再打")
    args = parser.parse_args(argv)

    print("[1/4] 检查旗帜素材")
    expected = check_assets()
    print("[2/4] 找 PyInstaller")
    command = find_pyinstaller()
    print("[3/4] 打包（onefile + 无控制台，按 main.spec）")
    build(command, clean=args.clean)
    print("[4/4] 核对包里素材")
    verify_bundle(expected)

    target = os.path.join(ROOT, "dist", "main.exe")
    size = os.path.getsize(target) / (1024 * 1024) if os.path.isfile(target) else 0.0
    print(f"\n完成：{target}  {size:.1f} MB")
    print("提示：素材随 onefile 解到临时目录、退出时自动清掉，这是正常的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
