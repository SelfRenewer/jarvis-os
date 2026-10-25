"""Arithmetic typed or said in chat («10 000 000 ÷ 1500», «15% от 2000», «2 в степени 10») — computed by code.

The message is rewritten into a tiny arithmetic language (numbers, + - * / **, parentheses, sqrt) and evaluated
on the Python AST with exact fractions — never `eval`. Unicode signs, operator words, grouped thousands
(«10 000»), decimal commas and «тыс/млн/млрд» are understood. A message with anything else in it is not a
calculation, so ordinary messages never land here. No LLM: the answer is instant.
"""

from __future__ import annotations

import ast
import math
import operator
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from typing import Any

from jarvis.core.numwords import spoken_numbers

Number = Fraction | Decimal  # Fraction = exact; Decimal = rounded (roots, fractional powers)

PRECISION = 40
MAX_DIGITS = 2000  # results longer than this are refused (2 ** 10**9 would hang the bot)
NBSP = "\u00a0"

_PREFIX = re.compile(
    r"^(?:а\s+)?(?:сколько\s+(?:будет|это|получится|выйдет)|посчитай|подсчитай|вычисли|рассчитай|реши|сколько"
    r"|calc|calculate)\b(?:\s+(?:мне|пожалуйста))?\s*[:,]?\s*",
    re.IGNORECASE,
)
_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}")
_GROUPED = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ \u00a0\u202f']\d{3})+)(?!\d)")
_NUM = r"\d+(?:\.\d+)?"
_ONE_AND_HALF = re.compile(r"\bполтор[аы]\b")
_SCALE = [
    (re.compile(rf"({_NUM})\s*(?:тыс(?:яч[аиу]?)?\.?|k|к)(?![a-zа-яё])"), "1000"),
    (re.compile(rf"({_NUM})\s*(?:млн\.?|миллион(?:а|ов)?)(?![a-zа-яё])"), "1000000"),
    (re.compile(rf"({_NUM})\s*(?:млрд\.?|миллиард(?:а|ов)?)(?![a-zа-яё])"), "1000000000"),
]
_WORDS = [
    (re.compile(r"\s*\b(?:умножить|умноженное|умножь|помножить|помноженное)\s+на\b\s*"), "*"),
    (
        re.compile(
            r"\s*\b(?:разделить|поделить|делить|деленное|делённое|поделенное|поделённое|раздели|подели)\s+на\b\s*"
        ),
        "/",
    ),
    (re.compile(r"\s*\b(?:плюс|прибавить|прибавь)\b\s*"), "+"),
    (re.compile(r"\s*\b(?:минус|отнять|отними|вычесть)\b\s*"), "-"),
    (re.compile(r"\s*\bв\s+степени\b\s*"), "**"),
    (re.compile(r"\s*\bв\s+квадрате\b"), "**2"),
    (re.compile(r"\s*\bв\s+кубе\b"), "**3"),
    (re.compile(r"\b(?:квадратный\s+)?корень\s+(?:квадратный\s+)?из\b\s*"), "√"),
    (_ONE_AND_HALF, "1.5"),
]
# Decimals said aloud (R33, 15.10: «8 млрд умножить на 0,3%» was heard on the Mac as «… на 0 3%»).
_PLACES = {"десят": 1, "сот": 2, "тысячн": 3}
_FRACTION = re.compile(r"(\d+)\s+цел\w*\s+(\d+)\s+(десят|сот|тысячн)\w*")  # «0 целых 3 десятых»
_POINT = re.compile(r"(\d+)\s+(?:запятая|точка)\s+(\d+)")  # «2 запятая 5»
_SPOKEN_ZERO = re.compile(r"(?<![\d.,])0[ \u00a0](\d+)(?!\d)")  # «ноль три», «0 3»: no number starts with «0 »
_SIGNS = str.maketrans({"÷": "/", "×": "*", "·": "*", "∙": "*", "−": "-", "–": "-", "—": "-"})
_OPERATOR = re.compile(r"[\d)]\s*(?:\*\*|[+\-*/])\s*[\d(s]|sqrt")
_BINARY: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class CalcError(ValueError):
    """The expression is arithmetic but cannot be computed (division by zero, a number too large)."""


@dataclass(frozen=True)
class Calculation:
    expression: str  # as the user wrote it (prefix and «=» removed)
    result: str  # Russian formatting: «6 666,67»
    approximate: bool

    def text(self) -> str:
        return f"🧮 {self.expression} {'≈' if self.approximate else '='} {self.result}"


def _decimals(text: str) -> str:
    """«0 целых 3 десятых» → 0.3, «2 запятая 5» → 2.5, «0 3» (a recognizer's «ноль три») → 0.3."""

    def fraction(m: re.Match[str]) -> str:
        places = _PLACES[m.group(3)]
        if len(m.group(2)) > places:
            return m.group(0)
        return f"{m.group(1)}.{m.group(2).zfill(places)}"

    return _SPOKEN_ZERO.sub(r"0.\1", _POINT.sub(r"\1.\2", _FRACTION.sub(fraction, text)))


def _as_heard(text: str) -> str:
    """What Jarvis understood, when dictation changed the numbers: «восемь миллиардов на 0 3%» → «8 миллиардов на
    0,3%» — the reply then shows the 0,3 (a misheard number is visible). Typed text («10 000 000») stays as written."""
    words = spoken_numbers(text)
    heard = _decimals(words)
    if heard == words and re.sub(r"\D", "", words) == re.sub(r"\D", "", text):
        return text
    return re.sub(r"(?<=\d)\.(?=\d)", ",", heard)


def _numbers(text: str) -> str:
    """«10 000» → «10000», «2,5» → «2.5», «1,5 млн» → «(1.5*1000000)», «полторы» → «1.5»."""
    value = _ONE_AND_HALF.sub("1.5", text)
    value = _GROUPED.sub(lambda m: re.sub(r"\D", "", m.group(1)), value)
    value = re.sub(r"(?<=\d),(?=\d)", ".", value)
    for pattern, factor in _SCALE:
        value = pattern.sub(rf"(\1*{factor})", value)
    return value


def _parse(expr: str) -> ast.expr | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)  # «7(999)» parses as a call and warns
        try:
            return ast.parse(expr, mode="eval").body
        except SyntaxError:
            return None


def to_python(text: str) -> tuple[str, str] | None:
    """(the expression as written, the arithmetic for the evaluator) — None when this is not pure arithmetic."""
    shown = _PREFIX.sub("", text.strip()).strip().rstrip("=?!. ").strip()
    if not shown or len(shown) > 200 or _DATE.fullmatch(shown):
        return None
    shown = _as_heard(shown)
    expr = _decimals(spoken_numbers(shown.lower().translate(_SIGNS)))  # dictated: numbers in words, decimals
    for pattern, sign in _WORDS:
        expr = pattern.sub(sign, expr)
    expr = _numbers(expr)
    expr = re.sub(r"(?<=[\d)])\s*[xх]\s*(?=[\d(])", "*", expr)  # «5 x 6», «5х6»
    expr = re.sub(r"\s:\s?|:\s", "/", expr)  # «10 : 2» (a bare «10:30» stays a time)
    expr = expr.replace("^", "**")
    expr = re.sub(rf"√\s*({_NUM})", r"sqrt(\1)", expr).replace("√", "sqrt")
    # Percent: «15% от 2000» → 15/100 × 2000; «2000 + 15%» → 2000 × 1.15 (as calculators do); «15%» → 0.15.
    expr = re.sub(rf"({_NUM})\s*(?:%|процент\w*)\s+от\s+", r"(\1/100)*", expr)
    expr = re.sub(rf"({_NUM})\s*([+\-])\s*({_NUM})\s*(?:%|процент\w*)", r"\1*(1\2\3/100)", expr)
    expr = re.sub(rf"({_NUM})\s*(?:%|процент\w*)", r"(\1/100)", expr).strip()
    if not re.fullmatch(r"[\d.+\-*/()\s]*", expr.replace("sqrt", "")) or not _OPERATOR.search(expr):
        return None
    if _parse(expr) is None:
        return None
    return shown, expr


def evaluate(expr: str) -> Number:
    tree = _parse(expr.strip())
    if tree is None:
        raise CalcError("не похоже на выражение")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        try:
            return _eval(tree, expr.strip())
        except ArithmeticError as exc:  # decimal overflow in exp/ln
            raise CalcError("слишком большое число") from exc


def _dec(value: Number) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(value.numerator) / Decimal(value.denominator)


def _eval(node: ast.AST, source: str) -> Number:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return Fraction(ast.get_source_segment(source, node) or str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        value = _eval(node.operand, source)
        return -value if isinstance(node.op, ast.USub) else value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "sqrt"
        and len(node.args) == 1
    ):
        return _sqrt(_eval(node.args[0], source))
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, source), _eval(node.right, source)
        if isinstance(node.op, ast.Pow):
            return _power(left, right)
        op = _BINARY.get(type(node.op))
        if op is None:
            raise CalcError("неподдерживаемая операция")
        if isinstance(node.op, ast.Div) and right == 0:
            raise CalcError("деление на ноль")
        if isinstance(left, Fraction) and isinstance(right, Fraction):
            return op(left, right)
        return op(_dec(left), _dec(right))
    raise CalcError("неподдерживаемая операция")


def _sqrt(value: Number) -> Number:
    if value < 0:
        raise CalcError("корень из отрицательного числа")
    if isinstance(value, Fraction):
        top, bottom = math.isqrt(value.numerator), math.isqrt(value.denominator)
        if top * top == value.numerator and bottom * bottom == value.denominator:
            return Fraction(top, bottom)
    return _dec(value).sqrt()


def _power(base: Number, exponent: Number) -> Number:
    if isinstance(exponent, Fraction) and exponent.denominator == 1:
        n = exponent.numerator
        if base == 0 and n < 0:
            raise CalcError("деление на ноль")
        if isinstance(base, Fraction):
            size = abs(n) * math.log10(max(abs(base.numerator), base.denominator, 2))
            if size > MAX_DIGITS:
                raise CalcError("слишком большое число")
            return base**n
        return base**n
    if base <= 0:
        raise CalcError("дробная степень отрицательного числа")
    return (_dec(base).ln() * _dec(exponent)).exp()


def format_number(value: Number) -> tuple[str, bool]:
    """(«6 666,67», approximate?) — Russian style: decimal comma, thousands grouped with a no-break space."""
    if isinstance(value, Fraction) and value.denominator == 1 and len(str(abs(value.numerator))) <= 21:
        return ("-" if value < 0 else "") + group_digits(str(abs(value.numerator))), False
    with localcontext() as ctx:
        ctx.prec = PRECISION
        number = _dec(value)
        if number == 0:
            return "0", isinstance(value, Decimal)
        magnitude = number.copy_abs().adjusted()  # the power of ten of the first significant digit
        if magnitude >= 21:
            mantissa = (number / Decimal(10) ** magnitude).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            return f"{_strip(f'{mantissa:f}').replace('.', ',')} × 10^{magnitude}", True
        places = 2 if magnitude >= 2 else 4 if magnitude >= 0 else min(12, 5 - magnitude)
        rounded = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return decimal_text(rounded), isinstance(value, Decimal) or rounded != number


def decimal_text(value: Decimal) -> str:
    """Decimal → «1 234,5» (trailing zeros dropped)."""
    text = _strip(f"{value:f}")
    sign = "-" if text.startswith("-") else ""
    whole, _, fraction = text.lstrip("-").partition(".")
    return sign + group_digits(whole) + ("," + fraction if fraction else "")


def _strip(text: str) -> str:
    return text.rstrip("0").rstrip(".") if "." in text else text


def group_digits(digits: str) -> str:
    head = len(digits) % 3 or 3
    return NBSP.join([digits[:head]] + [digits[i : i + 3] for i in range(head, len(digits), 3)])


def calculate(text: str) -> Calculation | None:
    """«10 000 000 ÷ 1500» → Calculation; None when the text is not arithmetic. Raises CalcError (÷ 0 …)."""
    parsed = to_python(text)
    if parsed is None:
        return None
    shown, expr = parsed
    result, approximate = format_number(evaluate(expr))
    return Calculation(shown, result, approximate)


def parse_amount(text: str) -> Decimal | None:
    """«10 000», «1,5 млн», «10к», «полторы тысячи» → Decimal (exact); None when it is not one number."""
    value = _numbers(text.strip().lower()).strip()
    if not re.fullmatch(rf"{_NUM}|\({_NUM}\*\d+\)", value):
        return None
    return _dec(evaluate(value))
