"""expr 层：表达式引擎。解析、校验、推导、求值表达式字符串，纯代码，没有 LLM 参与（ARCHITECTURE §3）。

其他模块只从这里 import（§1.2 第 3 条）。
"""

from litmus.expr.catalog import RATIO, field_catalog, operator_catalog, result_unit
from litmus.expr.collector import collect_fields, collect_lookback, compares_below
from litmus.expr.describer import describe
from litmus.expr.evaluator import Evaluation, ExprDataError, evaluate
from litmus.expr.operators import MAX_LOOKBACK, MAX_WINDOW
from litmus.expr.parser import ExprSyntaxError, Node, onset, parse
from litmus.expr.validator import ExprValidationError, Issue, ValidationResult, validate

__all__ = [
    "MAX_LOOKBACK",
    "RATIO",
    "MAX_WINDOW",
    "Evaluation",
    "ExprDataError",
    "ExprSyntaxError",
    "ExprValidationError",
    "Issue",
    "Node",
    "ValidationResult",
    "collect_fields",
    "collect_lookback",
    "compares_below",
    "describe",
    "evaluate",
    "field_catalog",
    "onset",
    "operator_catalog",
    "parse",
    "result_unit",
    "validate",
]
