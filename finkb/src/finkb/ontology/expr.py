"""极小的算术表达式求值器，供 L3 派生公式与约束校验共用。

为什么不 ``eval``：公式来自 YAML（人类可改），用 eval 等于把任意代码执行权
交给配置文件。这里只支持「数字 + 指标名 + + - * / + 括号 + 比较符」，
其余一律拒绝。

支持：
    表达式  (revenue - operating_cost) / revenue
    比较    total_assets == total_liabilities + total_equity
            current_assets <= total_assets
            -1 <= gross_margin <= 1          （链式比较按 and 展开）
    条件    net_profit > 0 and ocf / net_profit < 0.5
            not (x > 1)  /  a < 0 or b > 1   （and / or / not，括号可嵌套）

条件式只用于本体的数值定义类（``config/ontology_axioms.yaml``）与推理求值，
不参与事实抽取；变量名必须是 metric_id。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<op>[+\-*/(),<>]=?|==|!=)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_:.]*)"
    r"|(?P<str>'[^']*'))"
)

_CMP_OPS = {"==", "!=", "<", ">", "<=", ">="}


class ExprError(ValueError):
    pass


def tokenize(text: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise ExprError(f"无法解析的字符：{text[pos:pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


# --------------------------------------------------------------- AST
@dataclass
class Num:
    value: float


@dataclass
class Var:
    name: str


@dataclass
class Bin:
    op: str
    left: object
    right: object


@dataclass
class Neg:
    operand: object


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.t = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise ExprError("表达式意外结束")
        self.i += 1
        return tok

    def parse(self) -> object:
        node = self.expr()
        if self.peek() is not None:
            raise ExprError(f"多余的内容：{self.peek()}")
        return node

    def expr(self) -> object:
        node = self.term()
        while (tok := self.peek()) and tok[0] == "op" and tok[1] in ("+", "-"):
            self.take()
            node = Bin(tok[1], node, self.term())
        return node

    def term(self) -> object:
        node = self.unary()
        while (tok := self.peek()) and tok[0] == "op" and tok[1] in ("*", "/"):
            self.take()
            node = Bin(tok[1], node, self.unary())
        return node

    def unary(self) -> object:
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] == "-":
            self.take()
            return Neg(self.unary())
        if tok and tok[0] == "op" and tok[1] == "+":
            self.take()
            return self.unary()
        return self.atom()

    def atom(self) -> object:
        kind, val = self.take()
        if kind == "num":
            return Num(float(val))
        if kind == "name":
            return Var(val)
        if kind == "op" and val == "(":
            node = self.expr()
            if self.take() != ("op", ")"):
                raise ExprError("括号不匹配")
            return node
        if kind == "str":
            return Var(val[1:-1])
        raise ExprError(f"意外的记号 {val!r}")


def parse(text: str) -> object:
    return _Parser(tokenize(text)).parse()


def evaluate(node: object, env: Mapping[str, float]) -> float:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Var):
        if node.name not in env:
            raise ExprError(f"缺少变量 {node.name}")
        v = env[node.name]
        if v is None:
            raise ExprError(f"变量 {node.name} 为空")
        return float(v)
    if isinstance(node, Neg):
        return -evaluate(node.operand, env)
    if isinstance(node, Bin):
        a, b = evaluate(node.left, env), evaluate(node.right, env)
        if node.op == "+":
            return a + b
        if node.op == "-":
            return a - b
        if node.op == "*":
            return a * b
        if b == 0:
            raise ExprError("除零")
        return a / b
    raise ExprError(f"不支持的节点 {node!r}")


def deps(node: object) -> set[str]:
    if isinstance(node, Var):
        return {node.name}
    if isinstance(node, (Neg,)):
        return deps(node.operand)
    if isinstance(node, Bin):
        return deps(node.left) | deps(node.right)
    return set()


# ------------------------------------------------------- 比较式
@dataclass
class Comparison:
    left: object
    op: str
    right: object


def parse_comparison(text: str) -> list[Comparison]:
    """把 ``a == b + c`` / ``-1 <= x <= 1`` 拆成若干 Comparison。"""
    toks = tokenize(text)
    # 按比较符切段：a cmp b [cmp c ...]
    segments: list[list[tuple[str, str]]] = [[]]
    ops: list[str] = []
    for kind, val in toks:
        if kind == "op" and val in _CMP_OPS:
            ops.append(val)
            segments.append([])
        else:
            segments[-1].append((kind, val))
    if not ops:
        raise ExprError("比较式缺少比较符")
    nodes = [_Parser(s).parse() for s in segments]
    if len(nodes) != len(ops) + 1:
        raise ExprError(f"比较式结构不合法：{text}")
    return [Comparison(nodes[i], ops[i], nodes[i + 1]) for i in range(len(ops))]


def eval_comparison(cmp_: Comparison, env: Mapping[str, float], tol: float = 0.0) -> bool:
    a, b = evaluate(cmp_.left, env), evaluate(cmp_.right, env)
    op = cmp_.op
    if op == "==":
        return abs(a - b) <= tol
    if op == "!=":
        return abs(a - b) > tol
    if op == "<":
        return a < b
    if op == ">":
        return a > b
    if op == "<=":
        return a <= b + tol
    if op == ">=":
        return a >= b - tol
    raise ExprError(f"未知比较符 {op}")


def comparison_vars(text: str) -> set[str]:
    out: set[str] = set()
    for c in parse_comparison(text):
        out |= deps(c.left) | deps(c.right)
    return out


# ------------------------------------------------------- 布尔条件式
# defined_classes 的判定式需要 and / or / not（例如「净利润为正且现金流不足一半」）。
# 实现方式：先按括号深度切布尔运算符，再复用上面的比较式解析——这样算术部分
# 仍然只有一套词法/语法，不会因为多一个解析器而出现两套口径。
@dataclass
class BoolOp:
    op: str  # "and" / "or"
    operands: list[object]


@dataclass
class Not:
    operand: object


def _strip_outer_parens(text: str) -> str:
    t = text.strip()
    changed = True
    while changed and len(t) >= 2 and t[0] == "(" and t[-1] == ")":
        depth = 0
        balanced = True
        for i, ch in enumerate(t):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(t) - 1:
                    balanced = False
                    break
        if not balanced:
            break
        t = t[1:-1].strip()
    return t


def _split_top(text: str, keyword: str) -> list[str]:
    """在括号深度 0 处按 ``keyword`` 切分；keyword 前后必须有空格。"""
    out: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    kw = f" {keyword} "
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ExprError(f"括号不匹配：{text!r}")
        elif depth == 0 and text.startswith(kw, i):
            out.append(text[start:i].strip())
            start = i + len(kw) - 1  # 保留 keyword 后的空格位置给下一段
            i = start
            continue
        i += 1
    if depth != 0:
        raise ExprError(f"括号不匹配：{text!r}")
    if not out:
        return [text.strip()]
    out.append(text[start:].strip())
    return out


def _is_blank(segment: str) -> bool:
    return not segment.strip()


def parse_condition(text: str) -> object:
    """把 ``a > 0 and (b < 0 or c > 1)`` 解析成可求值的条件树。

    支持 and / or / not、括号、链式比较（``0 <= x <= 1``）与算术表达式。
    """
    if not text or not text.strip():
        raise ExprError("空条件式")
    t = _strip_outer_parens(text)

    parts = _split_top(t, "or")
    if len(parts) > 1:
        if any(_is_blank(p) for p in parts):
            raise ExprError(f"or 两侧缺表达式：{text!r}")
        return BoolOp("or", [parse_condition(p) for p in parts])

    parts = _split_top(t, "and")
    if len(parts) > 1:
        if any(_is_blank(p) for p in parts):
            raise ExprError(f"and 两侧缺表达式：{text!r}")
        return BoolOp("and", [parse_condition(p) for p in parts])

    low = t.lower()
    if low.startswith("not "):
        return Not(parse_condition(t[4:]))

    cmps = parse_comparison(t)
    return cmps[0] if len(cmps) == 1 else BoolOp("and", cmps)


def evaluate_condition(node: object, env: Mapping[str, float], tol: float = 0.0) -> bool:
    if isinstance(node, BoolOp):
        vals = [evaluate_condition(o, env, tol) for o in node.operands]
        return all(vals) if node.op == "and" else any(vals)
    if isinstance(node, Not):
        return not evaluate_condition(node.operand, env, tol)
    if isinstance(node, Comparison):
        return eval_comparison(node, env, tol)
    raise ExprError(f"不是条件节点：{node!r}")


def condition_deps(node: object) -> set[str]:
    if isinstance(node, BoolOp):
        out: set[str] = set()
        for o in node.operands:
            out |= condition_deps(o)
        return out
    if isinstance(node, Not):
        return condition_deps(node.operand)
    if isinstance(node, Comparison):
        return deps(node.left) | deps(node.right)
    return set()


def condition_vars(text: str) -> set[str]:
    return condition_deps(parse_condition(text))
