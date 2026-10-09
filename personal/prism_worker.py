"""Isolated Korean analysis worker; never imports trading or delivery modules.

Personal extension, 2026-10-09. Distributed under the repository AGPL-3.0.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import types


MAX_REPORT_BYTES = 500_000


def _isolate_delivery() -> None:
    """Suppress upstream maintenance sends inside this report-only process."""
    # dotenv preserves existing environment values, including empty strings.
    for key in ("OPS_ALERT_CHAT_ID", "OPS_ALERT_BOT_TOKEN", "OAUTH_ALERT_CHAT_ID", "OAUTH_ALERT_BOT_TOKEN"):
        os.environ[key] = ""
    os.environ["PRISM_DISABLE_SIGNAL_PUBLISH"] = "1"
    os.environ["PRISM_PARALLEL_REPORT"] = "false"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    logging.disable(logging.CRITICAL)

    async def no_delivery(*args, **kwargs):
        return False

    # analyze_stock calls this module for DART completeness notices. A local
    # no-delivery stub keeps the original source and its production behavior intact.
    alerts = types.ModuleType("prism_core.ops_alert")
    alerts.send_ops_alert = no_delivery
    sys.modules["prism_core.ops_alert"] = alerts


async def _report(symbol: str, name: str) -> str:
    # The import is intentionally inside the worker and the explicit request.
    from cores.analysis import analyze_stock

    reference_date = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    return await analyze_stock(
        company_code=symbol, company_name=name,
        reference_date=reference_date, language="ko",
    )


def _text_report(body: str) -> str:
    # Upstream embeds PNGs as large data URLs; this text report keeps its prose.
    body = re.sub(r'<img\b[^>]*\bsrc=[\"\']data:image/[^>]*>', "[차트 이미지 생략]", body, flags=re.IGNORECASE)
    body = re.sub(r'!\[[^\]]*\]\(data:image/[^)]+\)', "[차트 이미지 생략]", body, flags=re.IGNORECASE)
    body = re.sub(r'(?i)\bBearer\s+[a-z0-9._~+/-]{16,}', "Bearer [비공개]", body)
    body = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}', "[비공개]", body)
    body = re.sub(r'(?:bot)?\d{6,12}:[A-Za-z0-9_-]{30,}', "[비공개]", body)
    for key, value in os.environ.items():
        if any(part in key.lower() for part in ("secret", "token", "password", "api_key", "app_key")) and len(value) >= 12:
            body = body.replace(value, "[비공개]")
    return body


def main() -> int:
    if os.environ.get("PRISM_PERSONAL_AI_REQUEST") != "1" or len(sys.argv) != 4:
        return 2
    symbol, name, output_argument = sys.argv[1:]
    if not re.fullmatch(r"\d{6}", symbol) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        return 2
    try:
        output = Path(output_argument).resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if output.name != "report.md" or not output.parent.name.startswith("prism-personal-") or output.parent.parent != temporary_root:
            return 2
        if output.exists():
            return 2
        _isolate_delivery()
        body = asyncio.run(_report(symbol, name))
        if not isinstance(body, str) or not body.strip():
            return 3
        # The upstream engine can return placeholder prose after section failures.
        # Such a result must not become a successful personal report.
        if any(marker in body for marker in (
            "Analysis failed:", "Investment strategy analysis failed",
            "투자 전략 분석 실패", "요약 생성 중 오류가 발생했습니다.",
            "Problem occurred while generating analysis summary.",
        )):
            return 3
        body = _text_report(body)
        if len(body.encode("utf-8")) > MAX_REPORT_BYTES:
            return 3
        output.write_text(body, encoding="utf-8")
        return 0
    except Exception:
        # No exception text, tracebacks, credentials, or upstream logs are exposed.
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
