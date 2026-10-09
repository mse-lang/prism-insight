"""Offline coverage for opt-in analysis, process boundaries and job outcomes."""

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from personal import prism_bridge as bridge
from personal import prism_worker as worker


class FakeProcess:
    def __init__(self, command, **options):
        self.command = command
        self.options = options
        self.returncode = None
        self.pid = 987654

    def wait(self, timeout):
        Path(self.command[-1]).write_text("# 한국 종목 분석\n\n근거와 위험을 확인해 주세요.", encoding="utf-8")
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


def terminal_job(jobs, identifier):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = jobs.get(identifier)
        if result["status"] in ("completed", "failed"):
            return result
        time.sleep(0.005)
    raise AssertionError("offline job did not finish")


class PrismBridgeTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.save_report.side_effect = lambda report: dict(report, id=19)
        self.jobs = []

    def tearDown(self):
        for jobs in self.jobs:
            jobs.shutdown()

    def make_jobs(self):
        with patch.object(bridge, "prism_availability", return_value=(True, "준비됨")):
            jobs = bridge.PrismJobs(self.store, enabled=True)
        self.jobs.append(jobs)
        return jobs

    def test_disabled_import_and_constructor_do_not_spawn_or_import_engine(self):
        with patch.object(bridge.subprocess, "Popen") as spawn, patch.object(bridge, "prism_availability") as readiness:
            jobs = bridge.PrismJobs(self.store)
            self.jobs.append(jobs)
            self.assertFalse(jobs.enabled)
            readiness.assert_not_called()
            with self.assertRaises(ValueError):
                jobs.start("005930", "삼성전자")
            spawn.assert_not_called()

    def test_missing_dependencies_keep_explicit_flag_disabled(self):
        with patch.object(bridge.importlib.util, "find_spec", return_value=None):
            jobs = bridge.PrismJobs(self.store, enabled=True)
            self.jobs.append(jobs)
            self.assertFalse(jobs.enabled)
            self.assertIn("의존성", jobs.reason)

    def test_missing_configuration_is_unavailable_even_with_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cores").mkdir()
            (root / "cores" / "analysis.py").write_text("", encoding="utf-8")
            with patch.object(bridge.importlib.util, "find_spec", return_value=object()), patch.dict(os.environ, {"REPORT_MCP_CONFIG": "", "PRISM_MCP_CONFIG": ""}):
                enabled, reason = bridge.prism_availability(root)
            self.assertFalse(enabled)
            self.assertIn("설정", reason)

    def test_success_saves_bounded_report_and_returns_it_to_ui(self):
        jobs = self.make_jobs()
        processes = []

        def spawn(command, **options):
            process = FakeProcess(command, **options)
            processes.append(process)
            return process

        with patch.object(bridge.subprocess, "Popen", side_effect=spawn):
            queued = jobs.start("005930", "삼성전자")
            self.assertEqual(queued["status"], "queued")
            completed = terminal_job(jobs, queued["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["report_id"], 19)
        self.assertEqual(completed["report"]["kind"], "prism")
        process = processes[0]
        self.assertEqual(process.command[:3], [sys.executable, "-m", "personal.prism_worker"])
        self.assertFalse(process.options["shell"])
        self.assertEqual(process.options["stdout"], subprocess.DEVNULL)
        self.assertEqual(process.options["stderr"], subprocess.DEVNULL)
        self.assertEqual(process.options["env"]["PRISM_DISABLE_SIGNAL_PUBLISH"], "1")
        self.assertEqual(process.options["env"]["PRISM_PERSONAL_AI_REQUEST"], "1")
        self.assertFalse(Path(process.command[-1]).parent.exists())

    def test_raw_exception_never_reaches_ui_or_saved_reports(self):
        jobs = self.make_jobs()
        with patch.object(bridge.subprocess, "Popen", side_effect=RuntimeError("api_key=sk-super-secret-credentials")):
            result = terminal_job(jobs, jobs.start("005930", "삼성전자")["id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("sk-super", result["error"])
        self.store.save_report.assert_not_called()

    def test_timeout_stops_worker_and_does_not_save(self):
        jobs = self.make_jobs()
        process = FakeProcess(["unused"])
        process.wait = Mock(side_effect=subprocess.TimeoutExpired("worker", 600))

        def stop(target):
            target.returncode = -1

        with patch.object(bridge.subprocess, "Popen", return_value=process), patch.object(bridge, "_stop_process", side_effect=stop) as terminate:
            result = terminal_job(jobs, jobs.start("005930", "삼성전자")["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("10분", result["error"])
        terminate.assert_called_once_with(process)
        self.store.save_report.assert_not_called()

    def test_queue_has_one_worker_and_is_bounded(self):
        jobs = self.make_jobs()
        entered = threading.Event()
        release = threading.Event()
        spawn = Mock()

        class BlockedProcess(FakeProcess):
            def wait(self, timeout):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test worker timed out")
                return super().wait(timeout)

        spawn.side_effect = lambda command, **options: BlockedProcess(command, **options)
        with patch.object(bridge.subprocess, "Popen", spawn):
            identifiers = [jobs.start("005930", "삼성전자")["id"]]
            self.assertTrue(entered.wait(1))
            identifiers.extend(jobs.start("000660", "SK하이닉스")["id"] for _ in range(3))
            self.assertEqual(spawn.call_count, 1)
            with self.assertRaises(ValueError):
                jobs.start("035420", "네이버")
            release.set()
            for identifier in identifiers:
                self.assertEqual(terminal_job(jobs, identifier)["status"], "completed")

    def test_input_and_missing_job_fail_with_korean_validation(self):
        jobs = self.make_jobs()
        with self.assertRaises(ValueError):
            jobs.start("005930;bad", "삼성전자")
        with self.assertRaises(ValueError):
            jobs.get("missing")

    def test_shutdown_stops_active_process_cancels_queue_and_rejects_more_jobs(self):
        jobs = self.make_jobs()
        entered = threading.Event()
        release = threading.Event()

        class BlockedProcess(FakeProcess):
            def wait(self, timeout):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test worker timed out")
                return self.returncode

        def stop(target):
            target.returncode = -1
            release.set()

        with patch.object(bridge.subprocess, "Popen", side_effect=lambda command, **options: BlockedProcess(command, **options)), patch.object(bridge, "_stop_process", side_effect=stop) as terminate:
            running = jobs.start("005930", "삼성전자")
            self.assertTrue(entered.wait(1))
            queued = jobs.start("000660", "SK하이닉스")
            jobs.shutdown()
        self.assertEqual(jobs.get(running["id"])["status"], "failed")
        self.assertEqual(jobs.get(queued["id"])["status"], "failed")
        self.assertFalse(jobs.enabled)
        terminate.assert_called_once()
        self.store.save_report.assert_not_called()
        with self.assertRaises(RuntimeError):
            jobs.start("005930", "삼성전자")


class PrismWorkerTests(unittest.TestCase):
    def test_worker_uses_only_korean_analysis_and_kst_reference(self):
        upstream = types.ModuleType("cores.analysis")
        upstream.analyze_stock = AsyncMock(return_value="# 보고서")
        with patch.dict(sys.modules, {"cores.analysis": upstream}):
            self.assertEqual(asyncio.run(worker._report("005930", "삼성전자")), "# 보고서")
        arguments = upstream.analyze_stock.await_args.kwargs
        self.assertEqual(arguments["company_code"], "005930")
        self.assertEqual(arguments["language"], "ko")
        self.assertEqual(arguments["reference_date"], datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d"))

    def test_alert_transport_is_stubbed_and_settings_cannot_reload_env_tokens(self):
        with patch.dict(os.environ, {"OPS_ALERT_BOT_TOKEN": "must-not-send"}), patch.dict(sys.modules), patch.object(worker.logging, "disable"):
            worker._isolate_delivery()
            self.assertEqual(os.environ["OPS_ALERT_BOT_TOKEN"], "")
            self.assertFalse(asyncio.run(sys.modules["prism_core.ops_alert"].send_ops_alert("notice")))
            self.assertEqual(os.environ["PRISM_DISABLE_SIGNAL_PUBLISH"], "1")

    def test_text_output_removes_embedded_images_and_secret_shaped_values(self):
        source = '<img src="data:image/png;base64,AAAA">\n분석 sk-supersecretvalue123456\nBearer asecretbearer1234567890'
        output = worker._text_report(source)
        self.assertNotIn("base64", output)
        self.assertNotIn("supersecret", output)
        self.assertNotIn("asecretbearer", output)
        self.assertIn("분석", output)

    def test_worker_rejects_failure_placeholder_and_oversize_before_saving(self):
        for body in ("# 보고서\nAnalysis failed: news_analysis", "가" * 200_000):
            with self.subTest(body_length=len(body)), tempfile.TemporaryDirectory(prefix="prism-personal-") as directory:
                output = Path(directory) / "report.md"
                with patch.dict(os.environ, {"PRISM_PERSONAL_AI_REQUEST": "1"}), patch.object(sys, "argv", ["worker", "005930", "삼성전자", str(output)]), patch.object(worker, "_report", AsyncMock(return_value=body)), patch.object(worker, "_isolate_delivery"):
                    self.assertEqual(worker.main(), 3)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
