"""表达式校验：六件事，任一不过就不执行（ARCHITECTURE §3.4）。

1. 字段在白名单内：FIELDS 里属于这类标的的字段。research 用的内部列不对表达式开放
2. 算子在白名单内，参数个数、类型对
3. **未来函数**：窗口参数必须是正整数字面量——Ref 的 n > 0，从语言层面写不出引用未来数据的表达式
4. 窗口上限：单个 n <= 1000；整个表达式往前要读的行情 <= 1000 条
5. 结果类型：筛选、事件必须是条件（布尔），排序必须是数值
6. $close_raw 这类不能进时序算子的字段（除权日断崖下跌），不能出现在任何时序算子里面

另有 P0 的限制：Rank 不能放进时序算子里（§11）；事件表达式不能用 Rank（个股回看只有一只股票，没有池子可排）；
一个字段都没有的常量表达式不算条件。

一次列出所有问题，每条带位置和改写提示——大模型按报错改表达式时用得上。
标的能不能用（概念板块要能力探测通过）由调用方先用 ds.available_targets() 判断，这里不读数据。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from litmus.data import CONCEPT, FIELDS, INTERNAL_COLUMNS, STOCK, SW_INDUSTRY, names_for
from litmus.expr.collector import collect_lookback
from litmus.expr.operators import (
    ANY,
    BOOL,
    CROSS_SECTION,
    MAX_LOOKBACK,
    MAX_WINDOW,
    NUM,
    OPERATORS,
    TIMESERIES,
    WINDOW,
)
from litmus.expr.parser import Binary, Call, Field, Node, Number, Unary

#: 用途 → 表达式结果必须是什么类型
PURPOSES: dict[str, str] = {"filter": BOOL, "event": BOOL, "sort": NUM}

_TARGET_LABELS = {STOCK: "股票", SW_INDUSTRY: "申万行业", CONCEPT: "概念板块"}
_TYPE_LABELS = {NUM: "数值", BOOL: "条件（真/假）"}
_ARITHMETIC = frozenset({"+", "-", "*", "/"})
_ORDERING = frozenset({"<", "<=", ">", ">="})
_EQUALITY = frozenset({"==", "!="})
_LOGICAL = frozenset({"&", "|"})


@dataclass(frozen=True)
class Issue:
    message: str
    position: int | None = None

    def __str__(self) -> str:
        if self.position is None:
            return self.message
        return f"第 {self.position + 1} 个字符：{self.message}"


class ExprValidationError(ValueError):
    """表达式没通过校验。issues 是全部问题。"""

    def __init__(self, issues: tuple[Issue, ...]):
        super().__init__("；".join(str(issue) for issue in issues))
        self.issues = issues


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def raise_if_invalid(self) -> None:
        if self.issues:
            raise ExprValidationError(self.issues)


def validate(node: Node, target: str, purpose: str) -> ValidationResult:
    """校验表达式。target：stock / sw_industry / concept；purpose：filter / sort / event。"""
    if target not in _TARGET_LABELS:
        raise ValueError(f"不认识的标的类型 {target!r}")
    if purpose not in PURPOSES:
        raise ValueError(f"不认识的用途 {purpose!r}，可选 {list(PURPOSES)}")

    checker = _Checker(target)
    result = checker.type_of(node, None)
    issues = checker.issues
    expected = PURPOSES[purpose]
    if result is not None and result != expected:
        what = {"filter": "筛选条件", "event": "事件", "sort": "排序依据"}[purpose]
        issues.append(
            Issue(f"{what}要是{_TYPE_LABELS[expected]}，这个表达式算出来是{_TYPE_LABELS[result]}")
        )
    if not issues and not checker.fields_seen:  # 出过错的字段、算子不计入，别再连带报一条
        issues.append(Issue("表达式里没有任何字段，算不出因股票而异的结果"))
    if purpose == "event" and checker.rank_positions:
        issues.append(
            Issue(
                "个股回看的事件不能用 Rank：只有一只股票，没有股票池可排", checker.rank_positions[0]
            )
        )
    if not issues:
        lookback = collect_lookback(node)
        if lookback > MAX_LOOKBACK:
            issues.append(
                Issue(
                    f"这个表达式要往前读 {lookback} 条行情，超过上限 {MAX_LOOKBACK}（约 4 年）。"
                    f"EMA 按 8n 预热，n 最大 {MAX_LOOKBACK // 8}；更长的周期可以用 Mean"
                )
            )
    return ValidationResult(tuple(issues))


@dataclass
class _Checker:
    target: str
    issues: list[Issue] = field(default_factory=list)
    fields_seen: set[str] = field(default_factory=set)
    rank_positions: list[int] = field(default_factory=list)

    def add(self, message: str, position: int) -> None:
        self.issues.append(Issue(message, position))

    def type_of(self, node: Node, timeseries: Call | None) -> str | None:
        """返回 num / bool；出过错的子树返回 None，上层不再追着报连带的错。"""
        if isinstance(node, Number):
            return NUM
        if isinstance(node, Field):
            return self._field(node, timeseries)
        if isinstance(node, Unary):
            return self._unary(node, timeseries)
        if isinstance(node, Binary):
            return self._binary(node, timeseries)
        return self._call(node, timeseries)

    def _field(self, node: Field, timeseries: Call | None) -> str | None:
        definition = FIELDS.get(node.name)
        if definition is None or node.name in INTERNAL_COLUMNS:
            self.add(
                f"没有字段 ${node.name}{_suggest(node.name, names_for(self.target), '$')}",
                node.position,
            )
            return None
        if not definition.available_for(self.target):
            self.add(
                f"${node.name}（{definition.label}）不能用在{_TARGET_LABELS[self.target]}上",
                node.position,
            )
            return None
        if timeseries is not None and not definition.time_series_ok:
            self.add(
                f"${node.name}（{definition.label}）不能放进 {timeseries.name} 这类时序算子里："
                "不复权价在除权日会断崖下跌，均线、突破会出假信号；需要时序计算请用 $close",
                node.position,
            )
        self.fields_seen.add(node.name)
        return BOOL if definition.dtype == "bool" else NUM

    def _unary(self, node: Unary, timeseries: Call | None) -> str | None:
        operand = self.type_of(node.operand, timeseries)
        if operand is None:
            return None
        if node.op == "~":
            if operand != BOOL:
                self.add(
                    "~ 是对条件取反，只能用在条件上，比如 ~$is_st、~($close > 10)", node.position
                )
                return None
            return BOOL
        if operand != NUM:
            self.add("负号只能用在数值上", node.position)
            return None
        return NUM

    def _binary(self, node: Binary, timeseries: Call | None) -> str | None:
        left = self.type_of(node.left, timeseries)
        right = self.type_of(node.right, timeseries)
        if left is None or right is None:
            return None
        if node.op in _LOGICAL:
            if left != BOOL or right != BOOL:
                self.add(
                    f"{node.op} 两边都要是条件，比如 ($close > 10) {node.op} ($pe_ttm < 20)",
                    node.position,
                )
                return None
            return BOOL
        if node.op in _EQUALITY:
            if left != right:
                self.add(
                    f"{node.op} 两边类型要相同：条件和数值不能比，条件字段直接用或加 ~ 取反",
                    node.position,
                )
                return None
            return BOOL
        if left != NUM or right != NUM:
            hint = (
                "；数条件成立的天数用 Count"
                if node.op in _ARITHMETIC
                else "；条件字段直接用或加 ~ 取反"
            )
            self.add(f"{node.op} 两边都要是数值{hint}", node.position)
            return None
        return BOOL if node.op in _ORDERING else NUM

    def _call(self, node: Call, timeseries: Call | None) -> str | None:
        op = OPERATORS.get(node.name)
        if op is None:
            self.add(f"没有算子 {node.name}{_suggest(node.name, OPERATORS)}", node.position)
            return None
        if len(node.args) != len(op.args):
            self.add(
                f"{op.signature} 要 {len(op.args)} 个参数，这里给了 {len(node.args)} 个",
                node.position,
            )
            return None
        if op.kind == CROSS_SECTION:
            self.rank_positions.append(node.position)
            if timeseries is not None:
                self.add(
                    f"{op.name} 不能放进 {timeseries.name} 这类时序算子里（P0 不支持，比如「连续 3 天排名靠前」）；"
                    f"只看当天的排名可以写 {op.name}(x) > 0.9",
                    node.position,
                )
        inner = node if op.kind == TIMESERIES else timeseries

        types: list[str | None] = []
        for kind, arg in zip(op.args, node.args, strict=True):
            if kind == WINDOW:
                self._window(op.name, op.min_window, arg)
                types.append(NUM)
                continue
            actual = self.type_of(arg, inner)
            types.append(actual)
            if actual is None or kind == ANY or actual == kind:
                continue
            if kind == NUM:
                hint = (
                    f"；数条件成立的天数用 Count({_arg_text(arg)}, n)" if op.name == "Sum" else ""
                )
                self.add(f"{op.signature} 的参数要是数值，这里是条件{hint}", arg.position)
            else:
                self.add(
                    f"{op.signature} 的第一个参数要是条件，比如 {op.name}($close > Ref($close, 1), ...)",
                    arg.position,
                )
            types[-1] = None
        if any(t is None for t in types):
            return None
        if op.returns == "first":
            return types[0]
        if op.returns == "branch":
            if types[1] != types[2]:
                self.add("If 的两个分支类型要相同（都是数值，或都是条件）", node.position)
                return None
            return types[1]
        return op.returns

    def _window(self, name: str, minimum: int, arg: Node) -> None:
        if not (isinstance(arg, Number) and arg.is_int):
            self.add(
                f"{name} 的天数 n 必须是正整数，比如 {name}($close, 20)；不能是负数、小数或表达式——"
                "这条规则保证写不出引用未来数据的表达式",
                arg.position,
            )
            return
        value = int(arg.value)
        if value < minimum:
            self.add(f"{name} 的天数 n 至少为 {minimum}", arg.position)
        elif value > MAX_WINDOW:
            self.add(f"{name} 的天数 n 最大 {MAX_WINDOW}（约 4 年）", arg.position)


def _suggest(name: str, candidates, prefix: str = "") -> str:
    names = list(candidates)
    exact = [c for c in names if c.lower() == name.lower()]
    close = exact or difflib.get_close_matches(name, names, n=1, cutoff=0.6)
    return f"，是不是 {prefix}{close[0]}？" if close else ""


def _arg_text(node: Node) -> str:
    return f"${node.name}" if isinstance(node, Field) else "条件"
