"""用真实年报里抠出来的表格 HTML 做回归。

合成用例只能覆盖「我想到的形态」，这些 fixture 覆盖的是**真实排版**：
MinerU 输出没有 ``<th>``、没有 colspan 标记、表头靠词形和位置推断。
"""

from pathlib import Path

import pytest

from finkb.parser.markdown_blocks import html_table_layout

FIXTURES = Path(__file__).parent / "fixtures" / "tables"


def _layout(name: str):
    return html_table_layout((FIXTURES / name).read_text(encoding="utf-8"))


def test_plain_text_header_is_recognised() -> None:
    """释义表：三列全是中文词，全表无一格纯数字，表头仍要认出来。"""
    layout = _layout("plain_text_header_002594_2025_年报_8_0.html")
    assert layout.header == ["释义项", "指", "释义内容"]
    assert layout.rows[0] == ["本公司、公司、比亚迪", "指", "比亚迪股份有限公司"]


def test_alternating_key_value_row_is_not_a_header() -> None:
    """``股票简称|长安汽车、长安B|股票代码|000625、200625`` 是一行四格键值对。"""
    layout = _layout("two_column_kv_000625_2025_年报_5_0.html")
    assert not layout.header
    assert layout.rows[0][0] == "股票简称"


def test_period_row_is_a_header() -> None:
    """首格为空、第二格是年份的行是表头（期间表）。"""
    layout = _layout("single_pair_kv_002594_2025_年报_248_0.html")
    assert layout.header == ["", "2025年"]


def test_totally_plain_header_with_numeric_column() -> None:
    """``项目|期末余额|期初余额|理由``：无冒号、无 colspan，靠「后面含数字」认出。"""
    layout = _layout("ragged_group_row_601127_2025_年报_180_0.html")
    assert layout.header == ["项目", "期末余额", "期初余额", "理由"]
    assert layout.rows[0][1] == "38,923,447,136.30"


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.html")))
def test_no_fixture_yields_a_single_column(name: str) -> None:
    """任何真实表格都不该被解开成单列——那是 colspan 展开写错的信号。"""
    assert _layout(name).width >= 2
