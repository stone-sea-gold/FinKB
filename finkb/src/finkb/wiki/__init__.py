"""L2 知识层：wiki_vault 的页面模型、frontmatter 与索引维护。"""

from finkb.wiki.frontmatter import (
    FrontmatterError,
    build,
    dump_frontmatter,
    has_enough_sources,
    split_frontmatter,
    validate,
)
from finkb.wiki.vault import (
    Page,
    append_log,
    atomic_write,
    init_vault,
    iter_pages,
    read_page,
    rebuild_index,
    render_index,
    write_page,
)

__all__ = [
    "FrontmatterError",
    "Page",
    "append_log",
    "atomic_write",
    "build",
    "dump_frontmatter",
    "has_enough_sources",
    "init_vault",
    "iter_pages",
    "read_page",
    "rebuild_index",
    "render_index",
    "split_frontmatter",
    "validate",
    "write_page",
]
