#!/usr/bin/env python3
"""静态门禁统一入口：CI 与本地 pre-commit 共用同一份「查哪些文件」的逻辑。

为什么不让 YAML 里各写一遍 `find`：两边 glob 一旦漂移，就会出现
「本地过了 CI 挂了」或者更糟的「CI 一直在检查一个空目录」——
后者最阴，日志全绿但红线根本没执行。所以这里显式打印每个门禁的文件数。

用法：
    python .precommit/run_gates.py            # 查仓库
    python .precommit/run_gates.py --root DIR # 查指定根（测试用）
退出码：0 通过；1 有违规
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# (门禁名, 脚本, 目标目录)  —— 目标目录是相对被检查根的路径
GATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("no-captcha-code", "check_no_captcha_code.py", ("geopublish",)),
    ("no-naked-selectors", "check_no_naked_selectors.py", ("geopublish/adapters",)),
)


def discover(root: Path, subdirs: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        files.extend(sorted(base.rglob("*.py")))
    return files


def run_gate(name: str, script: str, files: list[Path]) -> bool:
    if not files:
        print(f"::notice::[{name}] 目标层尚未建立，0 个文件可查")
        return True

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / ".precommit" / script), *map(str, files)],
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑全部静态门禁")
    parser.add_argument("--root", default=str(REPO_ROOT), help="被检查的仓库根目录")
    args = parser.parse_args(argv)
    root = Path(args.root)

    failed: list[str] = []
    for name, script, subdirs in GATES:
        files = discover(root, subdirs)
        print(f"[{name}] {', '.join(subdirs)} → {len(files)} 个文件")
        if not run_gate(name, script, files):
            failed.append(name)

    if failed:
        print(f"\n门禁未通过：{', '.join(failed)}", file=sys.stderr)
        return 1
    print("\n全部门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
