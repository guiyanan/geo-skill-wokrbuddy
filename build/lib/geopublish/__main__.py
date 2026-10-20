"""`python -m geopublish` 入口（部署脚本更爱用这个，省得配 PATH）。"""

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover - 由 test_cli 的子进程用例覆盖
    sys.exit(main())
