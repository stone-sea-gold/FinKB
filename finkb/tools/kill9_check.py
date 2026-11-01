"""验证「原子写在 kill -9 下不产生半写页面」。

做法：子进程把 vault.atomic_write 的 os.replace 前拖住 30 秒并写标记文件；
父进程看到标记（说明已经进入替换窗口）后对子进程 SIGKILL；
然后检查目标页仍是完整旧版、可解析，同目录残留 mkstemp 临时文件。

用法：cd finkb && ../.venv/bin/python tools/kill9_check.py
退出码 0 = 通过。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

CHILD = r'''
import time
from pathlib import Path
from finkb.wiki import vault, frontmatter

root = Path(__VAULT__)
target = root / "summaries" / "killtest.md"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(
    "---\ntype: summary\ntitle: OLD\ndescription: 旧\nstatus: active\n"
    "updated_at: 2026-11-01T00:00:00\n---\n\n旧正文\n",
    encoding="utf-8",
)

import finkb.wiki.vault as v
real_replace = v.os.replace

def slow_replace(src, dst):
    Path(__MARKER__).write_text("yes", encoding="utf-8")
    time.sleep(30)
    return real_replace(src, dst)

v.os.replace = slow_replace
meta = frontmatter.build(
    page_type="summary", title="NEW", description="新",
    sources=["l0:002594_2025_年报"], company="002594", period="2025",
    tags=["test"], status="active",
)
vault.write_page(root, "summary", "killtest", meta, "新正文\n", status="active")
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="finkb-kill9-"))
    vault_root = tmp / "vault"
    marker = tmp / "reached_replace"
    script = tmp / "child.py"
    script.write_text(
        CHILD.replace("__VAULT__", repr(str(vault_root))).replace("__MARKER__", repr(str(marker))),
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    proc = subprocess.Popen([sys.executable, str(script)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 20
    while time.time() < deadline and not marker.exists():
        if proc.poll() is not None:
            out, err = proc.communicate()
            print("子进程提前退出，未进入替换窗口：", err.decode()[:800])
            return 1
        time.sleep(0.1)
    if not marker.exists():
        proc.kill()
        print("20 秒内没有进入替换窗口")
        return 1
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)

    page = vault_root / "summaries" / "killtest.md"
    from finkb.wiki import vault as vault_mod
    try:
        parsed = vault_mod.read_page(page)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL 目标页不可解析：{exc}")
        return 1
    leftovers = [p.name for p in page.parent.iterdir() if p.name != page.name]
    ok = parsed.meta.get("title") == "OLD" and any(n.endswith(".tmp") for n in leftovers)
    print(f"目标页 title={parsed.meta.get('title')!r}（应为 OLD）、残留临时文件={leftovers}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.path.insert(0, str(SRC))
    raise SystemExit(main())
