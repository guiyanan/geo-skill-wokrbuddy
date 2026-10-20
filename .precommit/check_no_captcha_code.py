#!/usr/bin/env python3
"""门禁（红线）：代码里不得出现任何验证码识别 / 绕过 / 破解逻辑。

PRD R13 规定遇验证码只停不绕。本检查在 git 提交时拦截违规代码，
防止后来者绕过合规约束。

用法： python .precommit/check_no_captcha_code.py <files...>
退出码： 0 通过，1 违规
"""
import re
import sys

# 拆词拼接，避免本文件自匹配
_TOKENS = ["captcha", "geetest", "yidun", "slider", "slide_", "verify_code",
           "verifycode", "bypass", "crack", "solve_", "anti_bot", "antibot",
           "yolo", "ddddocr", "muggle_ocr"]

_BANNED = re.compile("|".join(_TOKENS), re.IGNORECASE)

# 允许出现这些词的白名单场景：仅在注释/字符串里说明「不处理」
_ALLOW_SUFFIX = re.compile(r"#\s*(?:noqa|仅说明|不处理|red\s*line)", re.IGNORECASE)


def scan(path: str) -> list[tuple[int, str]]:
    hits = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if _ALLOW_SUFFIX.search(line):
                continue
            m = _BANNED.search(line)
            if m:
                hits.append((lineno, line.strip(), m.group(0)))
    return hits


def main() -> int:
    files = sys.argv[1:]
    if not files:
        return 0
    total = 0
    for path in files:
        for lineno, text, token in scan(path):
            print(f"{path}:{lineno}: 命中红线词 '{token}'")
            print(f"    {text}")
            print("    R13 规定：遇验证码只停不绕，不得实现任何处理逻辑")
            total += 1
    if total:
        print(f"\n共 {total} 处违规，提交已阻止")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
