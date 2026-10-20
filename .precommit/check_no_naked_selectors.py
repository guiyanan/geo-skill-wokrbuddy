#!/usr/bin/env python3
"""门禁：adapters/*.py 里不准出现裸 CSS 选择器。

所有选择器必须集中在 adapters/<platform>/selectors.json，
这样平台改版时只改 JSON，不用读代码。

用法： python .precommit/check_no_naked_selectors.py <files...>
退出码： 0 通过，1 违规
"""
import re
import sys

# 裸选择器的特征：字符串里出现 id/class/属性选择器语法，或常见元素关键词
PATTERNS = [
    r"[\"'][^\"']*#[A-Za-z][-\w]*[\s\"']",          # "#foo"
    r"[\"'][^\"']*\.[A-Za-z][-\w]+(?:__|--)[\w-]+",   # ".class__x" / ".class--x"
    r"[\"'][^\"']*\[(?:data-[-\w]+|id|class)[^\]]*\]",  # "[data-x]" / "[id=...]"
    r"[\"'][^\"']*(?:div|button|input|textarea|article)\s*>\s*[\w.#]",  # "div > .x"
]

SELECTOR_FILE_HINT = "请把它移到 selectors.json，代码里改用 sel('键名')"


def scan(path: str) -> list[tuple[int, str]]:
    hits = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            # 允许注释与文档说明
            if stripped.startswith("#"):
                continue
            for pat in PATTERNS:
                if re.search(pat, line):
                    hits.append((lineno, stripped))
                    break
    return hits


def main() -> int:
    files = sys.argv[1:]
    if not files:
        return 0
    total = 0
    for path in files:
        for lineno, text in scan(path):
            print(f"{path}:{lineno}: 疑似裸选择器 → {text}")
            print(f"    {SELECTOR_FILE_HINT}")
            total += 1
    if total:
        print(f"\n共 {total} 处违规，提交已阻止")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
