"""Deterministic long-entry conditions and observable evidence, without I/O.

Only completed prior weekday bars are used. A candidate is never permission to
place an order: the controller retains position, budget and broker safeguards.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

KST = timezone(timedelta(hours=9))
VERSION = "1.0.0"
_CATALOG = (
    ("breakout20", "20일 종가 돌파", 21, "마지막 확정 종가가 앞선 20개 종가와 20일 평균 위에 있는지 확인합니다."),
    ("trend_pullback", "추세 눌림 반등", 55, "상승 추세에서 평균선 근처의 종가 조정 뒤 반등을 확인합니다."),
    ("ma_recovery", "평균선 회복", 55, "상승 추세에서 확정 종가가 20일 평균선을 다시 넘어서는지 확인합니다."),
    ("consensus", "조건 합의", 55, "세 가지 종가 조건 중 두 가지 이상이 함께 충족되는지 확인합니다."),
)


def catalog():
    return [{"id": key, "name": name, "version": VERSION, "min_bars": minimum,
             "description": description} for key, name, minimum, description in _CATALOG]


def _number(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError
        result = Decimal(str(value).strip())
        if not result.is_finite() or not 0 < result <= Decimal("1e15"):
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise ValueError("종가와 현재 시세는 확인 가능한 양의 금액이어야 합니다.") from None


def _day(at):
    if isinstance(at, datetime):
        if at.tzinfo is None:
            raise ValueError("시간대가 있는 기준 시각이 필요합니다.")
        return at.astimezone(KST).date()
    if isinstance(at, date):
        return at
    raise ValueError("유효한 기준 날짜가 필요합니다.")


def _history(history, at, minimum, synthetic):
    if type(synthetic) is not bool or not isinstance(history, list):
        raise ValueError("검증 가능한 일봉 목록이 필요합니다.")
    today, previous, completed, excluded = _day(at), None, [], 0
    for row in history:
        try:
            if not isinstance(row, dict) or not isinstance(row.get("date"), str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", row["date"]):
                raise ValueError
            day = date.fromisoformat(row["date"])
        except ValueError:
            raise ValueError("일봉의 기준 날짜를 확인할 수 없습니다.") from None
        if day.weekday() >= 5 or day > today or (previous is not None and day <= previous):
            raise ValueError("일봉은 중복 없이 날짜순인 과거 평일 자료여야 합니다.")
        close = _number(row.get("close"))
        previous = day
        if day == today:
            excluded += 1
        else:
            completed.append((day, close))
    if len(completed) < minimum:
        raise ValueError(f"이 전략은 완료된 과거 일봉 {minimum}개 이상이 필요합니다.")
    if not synthetic and (today - completed[-1][0]).days > 7:
        raise ValueError("최근 완료 일봉이 오래되어 전략 계산을 보류합니다.")
    return completed, excluded


def _ma(values, length, ago=0):
    end = len(values) - ago
    return sum(values[end - length:end], Decimal(0)) / length


def _check(label, passed, value, phase="entry"):
    return {"label": label, "passed": bool(passed), "value": value, "phase": phase}


def _rules(values):
    close, ma20 = values[-1], _ma(values, 20)
    level = max(values[-21:-1])
    breakout = close > level and close > ma20
    results = {"breakout20": {"entry": breakout, "threshold": max(level, ma20),
                "checks": [_check("확정 종가가 앞선 20일 최고 종가보다 높음", close > level,
                                  {"close": float(close), "level": float(level)}),
                           _check("확정 종가가 20일 평균보다 높음", close > ma20,
                                  {"close": float(close), "ma20": float(ma20)})]}}
    if len(values) < 55:
        return results, {"ma20": float(ma20)}
    ma50, old_ma50 = _ma(values, 50), _ma(values, 50, 5)
    trend = ma20 > ma50 and ma50 > old_ma50
    trend_checks = [_check("20일 평균이 50일 평균보다 높음", ma20 > ma50,
                          {"ma20": float(ma20), "ma50": float(ma50)}, "trend"),
                    _check("50일 평균이 5거래일 전보다 높음", ma50 > old_ma50,
                           {"ma50": float(ma50), "ma50_5_sessions_ago": float(old_ma50)}, "trend")]
    a, b, c, d = values[-4:]
    ma20_b, ma20_c = _ma(values, 20, 2), _ma(values, 20, 1)
    falling, rebound = a > b > c, d > c
    support = b >= ma20_b and c >= ma20_c and c <= ma20_c * Decimal("1.03")
    above = d >= ma20
    pullback_checks = [*trend_checks,
        _check("반등 전 두 번 연속 종가 하락", falling, [float(a), float(b), float(c)]),
        _check("조정 종가가 20일 평균 이상이며 마지막 조정은 3% 이내", support,
               {"previous_close": float(c), "previous_ma20": float(ma20_c), "support_upper": float(ma20_c * Decimal("1.03"))}),
        _check("마지막 확정 종가가 직전 종가보다 높음", rebound, {"close": float(d), "previous_close": float(c)}),
        _check("반등 종가가 20일 평균 이상", above, {"close": float(d), "ma20": float(ma20)})]
    recovery = c <= ma20_c and d > ma20
    results["trend_pullback"] = {"entry": trend and falling and support and rebound and above,
                                  "threshold": max(ma20, c), "checks": pullback_checks}
    results["ma_recovery"] = {"entry": trend and recovery, "threshold": ma20,
                              "checks": [*trend_checks,
                                _check("직전 확정 종가는 당시 20일 평균 이하", c <= ma20_c,
                                       {"previous_close": float(c), "previous_ma20": float(ma20_c)}),
                                _check("마지막 확정 종가는 20일 평균 초과", d > ma20,
                                       {"close": float(d), "ma20": float(ma20)})]}
    return results, {"ma20": float(ma20), "ma50": float(ma50), "ma50_5_sessions_ago": float(old_ma50)}


def evaluate(history, at, strategy="breakout20", synthetic=False, current_price=None):
    selected = next((item for item in _CATALOG if item[0] == strategy), None)
    if selected is None:
        raise ValueError("지원하는 전략을 선택해주세요.")
    _, name, minimum, _ = selected
    completed, excluded = _history(history, at, minimum, synthetic)
    values = [close for _, close in completed]
    rules, metrics = _rules(values)
    current = _number(current_price) if current_price is not None else None
    ma20, breakout_level = _ma(values, 20), max(values[-21:-1])
    base = {"signal_date": completed[-1][0].isoformat(), "close": float(values[-1]),
            "breakout_level": float(breakout_level), "ma20": float(ma20),
            "breakout": rules["breakout20"]["entry"], "exit": values[-1] < ma20,
            "bars": [{"date": day.isoformat(), "close": float(close)} for day, close in completed[-21:]]}
    votes = []
    if strategy == "consensus":
        votes = [{"strategy": key, "name": next(item[1] for item in _CATALOG if item[0] == key),
                  "entry": rule["entry"], "current_entry": bool(rule["entry"] and current is not None and current > rule["threshold"]),
                  "entry_threshold": float(rule["threshold"])} for key, rule in rules.items()]
        accepted = sorted(rule["threshold"] for rule in rules.values() if rule["entry"])
        entry = len(accepted) >= 2
        threshold = accepted[1] if entry else max(rule["threshold"] for rule in rules.values())
        current_entry = sum(vote["current_entry"] for vote in votes) >= 2
        checks = [_check("세 조건 중 두 조건 이상이 확정 종가로 충족", entry,
                         {"passed": sum(vote["entry"] for vote in votes), "total": 3})]
        for vote in votes:
            checks.append(_check(vote["name"], vote["entry"], {"entry_threshold": vote["entry_threshold"]}))
    else:
        rule = rules[strategy]
        entry, threshold = rule["entry"], rule["threshold"]
        current_entry = bool(entry and current is not None and current > threshold)
        checks = list(rule["checks"])
    checks = [_check("완료된 과거 일봉 수", len(completed) >= minimum,
                     {"available": len(completed), "required": minimum}, "data"),
              _check("당일 일봉 제외", True, {"excluded": excluded, "signal_date": base["signal_date"]}, "data"),
              *checks,
              _check("현재 시세가 진입 기준을 유지", current_entry,
                     {"current_price": float(current) if current is not None else None, "entry_threshold": float(threshold)}, "current")]
    risks = []
    if current is None:
        risks.append("현재 시세가 확인되지 않아 현재 진입 조건은 통과하지 않았습니다.")
    elif entry and not current_entry:
        risks.append("과거 종가 조건이 충족됐지만 현재 시세가 진입 기준을 유지하지 못했습니다.")
    if strategy == "consensus":
        risks.append("서로 연관된 종가 조건의 합의이며 성공확률을 뜻하지 않습니다.")
    explanation = (f"{base['signal_date']}까지 완료된 종가 {len(completed)}개로 {name} 조건을 계산했습니다. "
                   + ("과거 진입 조건을 충족했습니다. " if entry else "과거 진입 조건을 충족하지 못해 대기합니다. ")
                   + ("현재 시세도 기준을 유지합니다. 최종 주문은 기존 위험 한도를 확인해야 합니다." if current_entry
                      else "현재 진입 조건은 통과하지 않았습니다."))
    return {**base, "strategy": strategy, "name": name, "version": VERSION, "min_bars": minimum,
            "available_bars": len(completed), "entry": bool(entry), "current_entry": bool(current_entry),
            "entry_threshold": float(threshold), "checks": checks, "risks": risks,
            "metrics": metrics, "votes": votes, "explanation": explanation}
