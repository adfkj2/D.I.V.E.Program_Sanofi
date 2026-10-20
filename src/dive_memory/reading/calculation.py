"""Bounded arithmetic over exact visible citations; never evaluates model code.

The model selects operands and operation. This module proves their surface
provenance and computes the result, NOT their semantic relevance/completeness.
Unsupported expressions raise ValueError, allowing a recorded reader fallback.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

from ..answering import (
    AnswerStatus,
    EvidenceCitation,
    GroundedAnswerDecision,
    validate_grounded_decision,
)

VERSION = "cited-calculation-v4"
_ONES = dict(
    zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
        range(20),
    )
)
_TENS = dict(
    zip(
        "twenty thirty forty fifty sixty seventy eighty ninety".split(),
        range(20, 100, 10),
    )
)
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000}
_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


def number(text):
    if not isinstance(text, str) or not 0 < len(text) <= 80:
        raise ValueError("numeric operand must be short source text")
    clean = text.strip().lower()
    if clean[:1] in {"$", "€", "£"}:
        clean = clean[1:]
    if re.fullmatch(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", clean):
        n = Decimal(clean.replace(",", ""))
    elif clean in {"half", "a half"}:
        n = Decimal("0.5")
    else:
        words = clean.replace("-", " ").split()
        if (
            not words
            or words[0] == "and"
            or words[-1] == "and"
            or any(
                w not in (_ONES.keys() | _TENS.keys() | _SCALES.keys() | {"and"})
                for w in words
            )
        ):
            raise ValueError("unsupported numeric expression")
        total = group = 0
        previous = None
        last_scale = float("inf")
        for word in words:
            if word == "and":
                if previous not in _SCALES:
                    raise ValueError("ambiguous number-word conjunction")
                previous = word
                continue
            if word in _ONES:
                if previous in _ONES or (
                    previous in _TENS and not 0 < _ONES[word] < 10
                ):
                    raise ValueError("ambiguous adjacent number words")
                group += _ONES[word]
            elif word in _TENS:
                if previous in _ONES or previous in _TENS:
                    raise ValueError("ambiguous number-word sequence")
                group += _TENS[word]
            elif word == "hundred":
                if not 0 < group < 10:
                    raise ValueError("invalid hundred multiplier")
                group *= 100
            else:
                if not group or _SCALES[word] >= last_scale:
                    raise ValueError("missing scale multiplier")
                total += group * _SCALES[word]
                group = 0
                last_scale = _SCALES[word]
            previous = word
        n = Decimal(total + group)
    if (
        not n.is_finite()
        or abs(n) > Decimal("1000000000000")
        or len(n.as_tuple().digits) > 18
    ):
        raise ValueError("numeric operand out of bounds")
    return n


@dataclass(frozen=True)
class DateRange:
    lo: date
    hi: date


def _date(text):
    try:
        return date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        raise ValueError("missing or malformed visible date") from None


def date_range(text, *, anchor=None):
    if not isinstance(text, str) or not 0 < len(text) <= 80:
        raise ValueError("date operand must be short source text")
    value = text.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        d = _date(value)
        return DateRange(d, d)
    match = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})", value)
    if match and match[1] in _MONTHS:
        d = date(int(match[3]), _MONTHS[match[1]], int(match[2]))
        return DateRange(d, d)
    match = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\s+(\d{4})", value)
    if match and match[2] in _MONTHS:
        d = date(int(match[3]), _MONTHS[match[2]], int(match[1]))
        return DateRange(d, d)
    match = re.fullmatch(r"([a-z]+)\s+(\d{4})", value)
    if match and match[1] in _MONTHS:
        year, month = int(match[2]), _MONTHS[match[1]]
        return DateRange(
            date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        )
    # No year inference, approximate-date invention, or event-to-event anchors.
    # A record timestamp is used only when the literal expression says 'ago'.
    d = _date(anchor)
    if value in {"today", "yesterday"}:
        d -= timedelta(days=value == "yesterday")
        return DateRange(d, d)
    if value == "last month":
        year, month = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
        return DateRange(
            date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        )
    match = re.fullmatch(r"(.+?)\s+(day|week|month|year)s?\s+ago", value)
    if not match:
        raise ValueError("unsupported or ambiguous relative date")
    n = number(match[1])
    if n < 0 or n != int(n) or n > 1200:
        raise ValueError("relative-date offset out of bounds")
    n = int(n)
    if match[2] in {"day", "week"}:
        d -= timedelta(days=n * (7 if match[2] == "week" else 1))
    else:
        months = n * (12 if match[2] == "year" else 1)
        year, month = divmod(d.year * 12 + d.month - 1 - months, 12)
        month += 1
        d = date(year, month, min(d.day, calendar.monthrange(year, month)[1]))
    return DateRange(d, d)


def _surface(value, quote):
    if not isinstance(value, str) or not value or value not in quote:
        raise ValueError("operand text is not in its exact quote")
    if not re.search(r"(?<![\w.,+\-])" + re.escape(value) + r"(?!\w|[.,]\d)", quote):
        raise ValueError("operand cuts through a larger word or number")


def _numeric_surface(value, quote):
    _surface(value, quote)
    # A shorter literal must not change 3,000 to 3, -3 to 3, or four hundred
    # to four. Inspect the maximal numeric phrase in the exact passage.
    tokens = "|".join(_ONES | _TENS | _SCALES | {"and": 0, "half": 0})
    first_tokens = "|".join(_ONES | _TENS | _SCALES | {"half": 0})
    expressions = re.compile(
        r"(?<![\w.])[$€£]?[-+]?\d[\d,.]*(?!\w)|"
        r"\ba\s+half\b|\b(?:" + first_tokens + r")(?:[\s-]+(?:" + tokens + r"))*\b",
        re.I,
    )
    target = number(value)
    for expression in expressions.finditer(quote):
        literal = expression.group().rstrip(".,")
        # Include trailing punctuation in the span, but not in the literal.
        end = expression.start() + len(literal)
        if value in literal and expression.start() < end:
            try:
                parsed = number(literal)
            except ValueError:
                continue
            if parsed == target and literal == value:
                return
    raise ValueError("operand is not a complete numeric expression")


def _format(n):
    # Never silently round small exact decimals to zero or discard precision.
    if n == n.to_integral_value():
        return format(n.to_integral_value(), "f")
    return format(n, "f").rstrip("0").rstrip(".")


def _divide(numerator, denominator):
    if not denominator:
        raise ValueError("division by zero")
    reduced = Fraction(numerator) / Fraction(denominator)
    divisor = reduced.denominator
    for factor in (2, 5):
        while divisor % factor == 0:
            divisor //= factor
    if divisor != 1:
        raise ValueError("calculation has a nonterminating decimal")
    return numerator / denominator


def _check_operation_intent(operation, question):
    # A quantity lookup is not automatically a delta between state updates.
    # These are necessary wording checks, not semantic understanding.
    required = {
        "difference": r"\b(?:difference|more|less|fewer|older|younger|increase|decrease|between|than|since|left|remaining|net|change)\b|相差|差多少|多了|少了|剩余",
        "mean": r"\b(?:average|mean)\b|平均",
        "ratio": r"\b(?:ratio|proportion|fraction|percent|percentage|per|rate|relative)\b|比例|占比|每",
    }.get(operation)
    if required and not re.search(required, str(question), re.I):
        raise ValueError("question does not establish the requested operation")


def _check_uncertainty(value, quote, visible_content, *, numeric=False):
    # Check the visible source around the quote as well: a shortened citation
    # must not turn "about five" into a precise five by cropping "about".
    qualifier = re.compile(
        r"\b(?:about|around|roughly|approximately|nearly|almost|at least|at most|"
        r"more than|less than|over|under|up to|between)\s*[$€£]?\s*$",
        re.I,
    )
    for passage in re.finditer(re.escape(quote), visible_content):
        for literal in re.finditer(re.escape(value), quote):
            offset = passage.start() + literal.start()
            before = visible_content[max(0, offset - 60) : offset]
            after = visible_content[offset + len(value) : offset + len(value) + 60]
            if qualifier.search(before):
                raise ValueError(
                    "approximate or bounded source cannot supply an exact operand"
                )
            if numeric and (
                re.search(r"\b(?:minus|negative)\s*$", before, re.I)
                or re.search(r"\d\s*[-–—/]\s*[$€£]?\s*$", before)
                or re.match(r"\s*(?:[-–—/]\s*[$€£]?\s*\d|to\s+\d)", after, re.I)
            ):
                raise ValueError(
                    "operand omits a sign, range endpoint or fraction component"
                )


def calculate(plan, request, *, distinct_scopes=False):
    if (
        not isinstance(plan, dict)
        or set(plan) != {"status", "operation", "operands", "unit"}
        or plan["status"] != "CALCULATE"
    ):
        raise ValueError("calculation plan schema mismatch")
    op, operands, unit = plan["operation"], plan["operands"], plan["unit"]
    numeric = {"sum", "mean", "difference", "product", "ratio"}
    dates = {
        "elapsed_days",
        "elapsed_weeks",
        "elapsed_months",
        "earliest",
        "latest",
        "date_order",
    }
    if not isinstance(op, str) or op not in numeric | dates | {"count"}:
        raise ValueError("unsupported operation")
    _check_operation_intent(op, request.question)
    if not isinstance(unit, str) or not re.fullmatch(r"[a-zA-Z %$€£/-]{0,40}", unit):
        raise ValueError("invalid unit label")
    if not isinstance(operands, list) or not 1 <= len(operands) <= 16:
        raise ValueError("requires one to sixteen operands")
    values: list[Decimal] = []
    date_values: list[DateRange] = []
    labels: list[str] = []
    citations: list[EvidenceCitation] = []
    seen: list[tuple[set[str], str, Decimal | str]] = []
    currencies = set()
    for operand in operands:
        if not isinstance(operand, dict):
            raise ValueError("operand must be object")
        if set(operand) == {"question_date"} and operand["question_date"] is True:
            if op not in {"elapsed_days", "elapsed_weeks", "elapsed_months"}:
                raise ValueError(
                    "question date only allowed for elapsed-time calculation"
                )
            d = _date(request.question_date)
            date_values.append(DateRange(d, d))
            labels.append("question date")
            continue
        required = {"index", "quote", "label"} | (set() if op == "count" else {"value"})
        if set(operand) != required:
            raise ValueError("operand schema mismatch")
        i, quote, label = operand["index"], operand["quote"], operand["label"]
        if type(i) is not int or not 0 <= i < len(request.items):
            raise ValueError("operand references hidden evidence")
        item = request.items[i]
        if (
            not isinstance(quote, str)
            or not 0 < len(quote) <= 2000
            or quote not in item.memory.content
        ):
            raise ValueError("operand quote is not exact visible evidence")
        if (
            not isinstance(label, str)
            or not 0 < len(label) <= 120
            or label not in quote
        ):
            raise ValueError("operand label must be present in its quote")
        _surface(label, quote)
        value = operand.get("value", label)
        if op in numeric:
            _numeric_surface(value, quote)
            if value[:1] in {"$", "€", "£"}:
                currencies.add(value[0])
        elif op != "count":
            _surface(value, quote)
        if op != "count":
            _check_uncertainty(value, quote, item.memory.content, numeric=op in numeric)
        refs = set(item.source_refs) or {item.memory.id}
        normalized = label.casefold().strip()
        if (
            distinct_scopes
            and op in {"sum", "mean", "count"}
            and any(normalized == prior_label for _, prior_label, _ in seen)
        ):
            raise ValueError(
                "repeated scope may be an update or duplicate, not an additive operand"
            )
        identity_value = number(value) if op in numeric else value
        for prior_refs, prior_label, prior_value in seen:
            if not refs.intersection(prior_refs):
                continue
            if normalized == prior_label and identity_value == prior_value:
                raise ValueError("repeated operand from the same provenance")
            if op == "count" and (
                normalized in prior_label or prior_label in normalized
            ):
                raise ValueError("overlapping entity labels from the same provenance")
        seen.append((refs, normalized, identity_value))
        citations.append(
            EvidenceCitation(item.memory.id, quote, tuple(item.source_refs))
        )
        labels.append(label)
        if op in numeric:
            values.append(number(value))
        elif op == "count":
            values.append(Decimal(1))
        else:
            # valid_from is visible; observed_at is not. Hidden metadata may
            # veto an unsafe anchor, but can never supply a replacement date.
            anchor = item.memory.valid_from
            if (
                item.memory.observed_at
                and anchor
                and _date(item.memory.observed_at) != _date(anchor)
            ):
                anchor = None
            date_values.append(date_range(value, anchor=anchor))
    if not citations:
        raise ValueError("calculation requires source evidence")
    if len(currencies) > 1:
        raise ValueError("currency conversion is unsupported")
    if currencies:
        symbol = next(iter(currencies))
        allowed = {
            "$": {"$", "dollars", "USD"},
            "€": {"€", "euros", "EUR"},
            "£": {"£", "pounds", "GBP"},
        }[symbol]
        if (
            op == "product"
            or (op == "ratio" and unit)
            or (op != "ratio" and unit and unit not in allowed)
        ):
            raise ValueError("currency result unit is unsupported")
        if not unit and op != "ratio":
            unit = symbol
    with localcontext() as context:
        context.prec = 200
        if op in {"sum", "count"}:
            answer = _format(sum(values))
        elif op == "mean":
            result = _divide(sum(values), Decimal(len(values)))
            answer = _format(result)
        elif op in {"difference", "ratio", "product"}:
            if len(values) != 2:
                raise ValueError("binary operation needs exactly two operands")
            if op == "ratio" and not values[1]:
                raise ValueError("division by zero")
            result = (
                values[0] - values[1]
                if op == "difference"
                else values[0] * values[1]
                if op == "product"
                else _divide(values[0], values[1])
            )
            answer = _format(result)
        elif op.startswith("elapsed_"):
            if len(date_values) != 2:
                raise ValueError("elapsed time needs start then end")
            start, end = date_values
            if end.hi < start.lo:
                raise ValueError("elapsed-time operands reversed")
            if op == "elapsed_months":
                # Calendar-month distance, not an undocumented days/30 estimate.
                if (start.lo.month, start.lo.year) != (
                    start.hi.month,
                    start.hi.year,
                ) or (end.lo.month, end.lo.year) != (end.hi.month, end.hi.year):
                    raise ValueError("month range crosses a month boundary")
                answer = str(
                    (end.lo.year - start.lo.year) * 12 + end.lo.month - start.lo.month
                )
            else:
                if start.lo != start.hi or end.lo != end.hi:
                    raise ValueError("day/week calculation requires precise dates")
                days = (end.lo - start.lo).days
                if op == "elapsed_weeks" and days % 7:
                    raise ValueError("week duration has a nonterminating decimal")
                answer = _format(Decimal(days) / (7 if op == "elapsed_weeks" else 1))
            unit = {
                "elapsed_days": "days",
                "elapsed_weeks": "weeks",
                "elapsed_months": "calendar months",
            }[op]
        else:
            ordered = sorted(range(len(date_values)), key=lambda i: date_values[i].lo)
            if any(
                date_values[a].hi >= date_values[b].lo
                for a, b in zip(ordered, ordered[1:])
            ):
                raise ValueError("overlapping or tied dates do not prove strict order")
            if op == "earliest":
                answer = labels[ordered[0]]
            elif op == "latest":
                answer = labels[ordered[-1]]
            else:
                answer = ", then ".join(labels[i] for i in ordered)
            unit = ""
    decision = GroundedAnswerDecision(
        AnswerStatus.ANSWER,
        (answer + " " + unit).strip(),
        "CITED_CALCULATION_RESULT",
        tuple(citations),
        policy_version=VERSION,
    )
    decision = validate_grounded_decision(decision, request.items)
    if decision.reason_code == "UNSUPPORTED_READER_OUTPUT":
        raise ValueError("calculation citation validation failed")
    return decision
