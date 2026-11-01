"""L2 主检索：SQLite FTS5 + 中文别名扩展（ADR-002）。"""

from finkb.search.index import build_index, expand_query, search

__all__ = ["build_index", "expand_query", "search"]
