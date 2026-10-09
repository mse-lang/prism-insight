"""Optional, explicit-request bridge to the original Korean report engine.

Personal extension, 2026-10-09. Distributed under the repository AGPL-3.0.
Importing this module never imports PRISM, authenticates, or starts a job.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import uuid


REPOSITORY = Path(__file__).resolve().parents[1]
MAX_REPORT_BYTES = 500_000
JOB_TIMEOUT_SECONDS = 600
MAX_PENDING_JOBS = 4
KST = timezone(timedelta(hours=9))
FAILURE_MESSAGE = (
    "PRISM 보고서를 생성하지 못했습니다. 원본 requirements.txt 설치와 "
    "MCP 분석 설정 및 OpenAI 인증을 확인한 뒤 다시 요청해 주세요."
)


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _configured_key(value: str) -> bool:
    value = value.strip().strip("\"'")
    return len(value) > 10 and not any(
        marker in value.lower()
        for marker in ("example", "your-", "your_", "placeholder", "xxxx", "<", "${")
    )


def _has_api_key(root: Path) -> bool:
    if _configured_key(os.environ.get("OPENAI_API_KEY", "")):
        return True
    try:
        text = (root / ".env").read_text(encoding="utf-8")
        if any(_configured_key(match) for match in re.findall(r"(?m)^\s*(?:export\s+)?OPENAI_API_KEY\s*=\s*([^\r\n#]+)", text)):
            return True
    except (OSError, UnicodeError):
        pass
    try:
        import yaml

        secrets = yaml.safe_load((root / "mcp_agent.secrets.yaml").read_text(encoding="utf-8")) or {}
        openai = secrets.get("openai") or {}
        return isinstance(openai, dict) and _configured_key(str(openai.get("api_key") or ""))
    except Exception:
        pass
    return False


def prism_availability(root: Path = REPOSITORY) -> tuple[bool, str]:
    """Inspect prerequisites without importing the engine or returning secrets."""
    dependencies = ("dotenv", "yaml", "openai", "agents", "mcp", "pandas", "numpy", "matplotlib", "seaborn", "mplfinance", "tenacity")
    for name in dependencies:
        try:
            present = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            present = False
        if not present:
            return False, "PRISM 분석 의존성이 없습니다. 원본 requirements.txt를 설치해 주세요."
    if not (root / "cores" / "analysis.py").is_file():
        return False, "원본 PRISM 한국 분석 모듈을 찾을 수 없습니다."
    explicit_config = os.environ.get("REPORT_MCP_CONFIG") or os.environ.get("PRISM_MCP_CONFIG")
    configs = (Path(explicit_config),) if explicit_config else (
        root / "cores" / "llm" / "mcp_servers.yaml", root / "mcp_agent.config.yaml",
    )
    if not any((path if path.is_absolute() else root / path).is_file() for path in configs):
        return False, "PRISM MCP 분석 설정 파일을 확인한 뒤 서버를 다시 시작해 주세요."
    if not _has_api_key(root):
        return False, "OpenAI API 인증을 설정한 뒤 서버를 다시 시작해 주세요."
    return True, "PRISM 분석을 요청할 수 있습니다. 실제 연결은 보고서 요청 시 확인됩니다."


def _stop_process(process: subprocess.Popen) -> None:
    """Stop only the worker process tree created by this bridge."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=False, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


class PrismJobs:
    """One report at a time, opt-in only, with sanitized public job state."""

    def __init__(self, store, enabled: bool = False):
        self.store = store
        self.enabled = False
        self.reason = "PRISM 분석은 --enable-prism 옵션으로 켤 수 있습니다."
        if enabled:
            self.enabled, self.reason = prism_availability()
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._process: subprocess.Popen | None = None
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="personal-prism")

    def start(self, symbol: str, name: str) -> dict:
        if not isinstance(symbol, str) or not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("PRISM 분석은 한국 주식의 6자리 종목코드로 요청해 주세요.")
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise ValueError("종목명을 확인해 주세요.")
        with self._lock:
            if self._closed:
                raise RuntimeError("분석 서버가 종료 중입니다.")
            if not self.enabled:
                raise ValueError(self.reason)
            pending = sum(job["status"] in ("queued", "running") for job in self._jobs.values())
            if pending >= MAX_PENDING_JOBS:
                raise ValueError("분석 요청이 진행 중입니다. 완료 후 다시 요청해 주세요.")
            for job_id in list(self._jobs):
                if len(self._jobs) < 64:
                    break
                if self._jobs[job_id]["status"] in ("completed", "failed"):
                    del self._jobs[job_id]
            job = {
                "id": uuid.uuid4().hex, "symbol": symbol, "name": name.strip(),
                "status": "queued", "created_at": _now(), "report_id": None,
                "error": None,
            }
            self._jobs[job["id"]] = job
            result = dict(job)
            self._executor.submit(self._run, job["id"])
            return result

    def get(self, job_id: str) -> dict:
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError("분석 요청을 찾을 수 없습니다.")
            return dict(self._jobs[job_id])

    def _update(self, job_id: str, **values) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _run(self, job_id: str) -> None:
        with self._lock:
            if self._closed:
                self._jobs[job_id].update(status="failed", error="서버 종료로 분석이 취소되었습니다.")
                return
            job = dict(self._jobs[job_id])
            self._jobs[job_id].update(status="running", started_at=_now())
        process = None
        try:
            with tempfile.TemporaryDirectory(prefix="prism-personal-") as directory:
                output = Path(directory) / "report.md"
                environment = os.environ.copy()
                environment["PRISM_PERSONAL_AI_REQUEST"] = "1"
                environment["PRISM_PARALLEL_REPORT"] = "false"
                environment["PRISM_DISABLE_SIGNAL_PUBLISH"] = "1"
                environment["PRISM_MCP_PYTHON"] = sys.executable
                environment["PRISM_REPO_ROOT"] = str(REPOSITORY)
                options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
                with self._lock:
                    if self._closed:
                        raise RuntimeError("closed")
                    process = subprocess.Popen(
                        [sys.executable, "-m", "personal.prism_worker", job["symbol"], job["name"], str(output)],
                        cwd=str(REPOSITORY), env=environment, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        shell=False, **options,
                    )
                    self._process = process
                try:
                    return_code = process.wait(timeout=JOB_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    _stop_process(process)
                    self._update(job_id, status="failed", error="분석 시간이 10분을 초과했습니다. 인증 및 분석 설정을 확인한 뒤 다시 요청해 주세요.", finished_at=_now())
                    return
                if return_code != 0 or not output.is_file() or output.stat().st_size > MAX_REPORT_BYTES:
                    raise RuntimeError("worker failed")
                body = output.read_text(encoding="utf-8")
                if not body.strip():
                    raise RuntimeError("empty report")
            # Publish completion only after temporary report cleanup is complete.
            with self._lock:
                if self._closed:
                    raise RuntimeError("closed")
                report = self.store.save_report({
                    "symbol": job["symbol"], "title": f'{job["name"]} PRISM AI 분석',
                    "body": body, "kind": "prism", "created_at": _now(), "metrics": {},
                })
                report_id = report.get("id") if isinstance(report, dict) else report
                self._jobs[job_id].update(status="completed", report=report, report_id=report_id, finished_at=_now())
        except Exception:
            self._update(job_id, status="failed", error=FAILURE_MESSAGE, finished_at=_now())
        finally:
            if process is not None and process.poll() is None:
                _stop_process(process)
            with self._lock:
                if self._process is process:
                    self._process = None

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self.enabled = False
            process = self._process
            for job in self._jobs.values():
                if job["status"] == "queued":
                    job.update(status="failed", error="서버 종료로 분석이 취소되었습니다.", finished_at=_now())
        if process is not None:
            _stop_process(process)
        self._executor.shutdown(wait=True, cancel_futures=True)
