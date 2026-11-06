"""``finkb-mcp`` 入口：先固定本地知识库路径，再启动 MCP stdio server。

用法::

    finkb-mcp                         # 用默认知识库（源码树或 ~/.finkb）
    finkb-mcp --home ~/my-finkb       # 指定知识库目录
    FINKB_HOME=~/my-finkb finkb-mcp   # 等价写法

必须在 import 业务模块之前设置 FINKB_HOME——各模块的路径常量在导入时确定。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    argv = sys.argv[1:]
    home: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--home" and i + 1 < len(argv):
            home = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--home="):
            home = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    if home:
        os.environ["FINKB_HOME"] = str(Path(home).expanduser().resolve())
    sys.argv = [sys.argv[0], *rest]
    from finkb.qa.mcp_server import main as serve

    serve()


if __name__ == "__main__":  # pragma: no cover
    main()
