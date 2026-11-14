import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from service.runtime_logs import list_runs, read_run
from service.utils import log_paths
from service.utils.runtime_log import trace_run
from service.utils.transcript_log import TranscriptLogger


class GenerationRuntimeLogTests(unittest.TestCase):
    job = "adea374135994857a7937c6813cd69c6"
    other_job = "b" * 32

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.logs = self.project / "logs"
        self.logs.mkdir(parents=True)
        self.log_home = patch.object(log_paths, "PROJECTS_DIR", Path(self.temp.name) / "chats")
        self.log_home.start()
        self.addCleanup(self.log_home.stop)

    def write_records(self, name, records):
        (self.logs / name).write_text(
            "\n\n".join(json.dumps(record, ensure_ascii=False, indent=2) for record in records) + "\n\n",
            encoding="utf-8",
        )

    def record(self, second, **fields):
        return {"timestamp": datetime(2026, 11, 3, 15, 0, second).astimezone(timezone.utc).isoformat(),
                "job_id": self.job, "plan_id": "plan-1", "stage": "itinerary_agent", **fields}

    def lifecycle(self, name="generation.log", end="failed"):
        (self.logs / name).write_text(
            f"2026-11-03 15:00:00,000 INFO started plan_id=plan-1 job_id={self.job}\n"
            f"2026-11-03 15:00:00,100 INFO checkpoint plan_id=plan-1 job_id={self.job} stage=prepare_draft next=itinerary_agent attempts=0\n"
            f"2026-11-03 15:00:04,000 ERROR {end} plan_id=plan-1 job_id={self.job}\n",
            encoding="utf-8",
        )

    def test_historical_generation_merges_model_map_and_traceback_in_time_order(self):
        self.lifecycle()
        self.write_records("model.requests.log", [self.record(1, body={"model": "qwen2.5:7b",
            "messages": [{"role": "user", "content": {"ordered_places": []}}]}, call_id="model-1")])
        self.write_records("model.responses.log", [self.record(2, body={"model": "qwen2.5:7b",
            "message": {"content": {"days": [{"day": 1, "visits": [{"position": 1}]}]}},
            "prompt_eval_count": 12, "eval_count": 7}, duration_ms=1000)])
        self.write_records("api.requests.log", [self.record(1, stage="search_places", direction="outbound",
            url="https://restapi.amap.com/v3/place/text", params={"keywords": "独山子大峡谷", "key": "private-map-key"}),
            self.record(1, job_id=self.other_job, body={"private": "other task"}),
            {"timestamp": self.record(1)["timestamp"], "direction": "inbound", "body": {"job_id": self.job}}])
        self.write_records("api.responses.log", [self.record(2, stage="search_places", direction="outbound",
            status=200, body={"status": "0", "info": "INVALID_PARAMS", "infocode": "20000"})])
        self.write_records("errors.log", [self.record(3, message="地点位置 1 重复或越界，已安排位置：[]",
            traceback="ValueError: 地点位置 1 重复或越界", attempt=1)])

        runs = list_runs(self.project)["runs"]
        self.assertEqual([run["runId"] for run in runs if run["status"] == "failed"], [self.job])
        detail = read_run(self.project, self.job)
        self.assertEqual(detail["run"]["source"], "generation")
        self.assertEqual(detail["run"]["planId"], "plan-1")
        self.assertEqual(detail["run"]["model"], "qwen2.5:7b")
        self.assertEqual(detail["run"]["durationMs"], 4000)
        events = detail["events"]
        self.assertEqual(events[0]["type"], "run_start")
        self.assertEqual(events[-1]["status"], "failed")
        self.assertEqual([e["timestamp"] for e in events], sorted(e["timestamp"] for e in events))
        response = next(e for e in events if e["type"] == "model_response")
        self.assertEqual(response["usage"], {"inputTokens": 12, "outputTokens": 7})
        self.assertEqual(response["durationMs"], 1000)
        self.assertTrue(next(e for e in events if e["type"] == "api_response")["isError"])
        encoded = json.dumps(events, ensure_ascii=False)
        self.assertIn("ValueError: 地点位置", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertNotIn("private-map-key", encoded)
        self.assertNotIn("other task", encoded)

    def test_job_filter_and_conversation_filter_keep_tasks_separate(self):
        self.lifecycle()
        self.write_records("errors.log", [self.record(1, job_id=self.other_job, message="other")])
        transcript = TranscriptLogger(self.project, session_id="chat-1")
        with trace_run(transcript, model="test", text="chat question") as run:
            run.finish("completed", "answer")
        self.assertEqual(len(list_runs(self.project)["runs"]), 3)
        self.assertEqual([r["runId"] for r in list_runs(self.project, job_id=self.job)["runs"]], [self.job])
        self.assertEqual([r["input"] for r in list_runs(self.project, conversation_id="chat-1")["runs"]], ["chat question"])
        self.assertIsNone(read_run(self.project, self.job, conversation_id="chat-1"))
        self.assertEqual(list_runs(self.project, job_id=self.job, status="completed")["runs"], [])
        with self.assertRaises(ValueError):
            list_runs(self.project, job_id="../secret")

    def test_run_pages_are_filtered_and_do_not_load_event_details(self):
        self.lifecycle()
        self.write_records("model.requests.log", [self.record(2, job_id=self.other_job,
            body={"model": "test-model", "messages": [{"content": "large prompt"}]})])
        with patch("service.generation_logs._wire_event", side_effect=AssertionError("loaded event details")):
            first = list_runs(self.project, page_size=1)
            second = list_runs(self.project, page=2, page_size=1)
            last = list_runs(self.project, page=99, page_size=1)
            failed = list_runs(self.project, status="failed", page=2, page_size=1)
        self.assertEqual(first["total"], 2)
        self.assertEqual([r["runId"] for r in first["runs"]], [self.other_job])
        self.assertEqual(first["runs"][0]["model"], "test-model")
        self.assertEqual([r["runId"] for r in second["runs"]], [self.job])
        self.assertEqual(last, second)
        self.assertEqual((failed["page"], failed["total"]), (1, 1))

    def test_rotated_complete_records_survive_corruption_and_an_unfinished_write(self):
        self.lifecycle("generation.log.1", end="completed")
        self.write_records("model.responses.log.1", [self.record(2, body={"model": "openai-model",
            "choices": [{"message": {"content": "answer"}}], "usage": {"prompt_tokens": 8, "completion_tokens": 3}})])
        with (self.logs / "model.responses.log.1").open("a", encoding="utf-8") as stream:
            stream.write("{broken}\n\n")
        (self.logs / "model.responses.log").write_text('{"timestamp": "unfinished', encoding="utf-8")
        detail = read_run(self.project, self.job)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["run"]["status"], "completed")
        response = next(e for e in detail["events"] if e["type"] == "model_response")
        self.assertEqual(response["content"], "answer")
        self.assertEqual(response["usage"], {"inputTokens": 8, "outputTokens": 3})

    def test_bounded_tail_read_reports_truncation_and_retains_newest_record(self):
        self.write_records("errors.log", [self.record(1, message="old" * 500), self.record(2, message="newest")])
        with patch("service.generation_logs.MAX_STREAM_BYTES", 600):
            result = list_runs(self.project)
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["runs"]), 1)
            detail = read_run(self.project, self.job)
        self.assertEqual([e["message"] for e in detail["events"]], ["newest"])

    def test_malformed_model_fields_preserve_original_response_bodies(self):
        bodies = [
            {"message": "Internal Server Error"},
            {"choices": [None]},
            {"message": {"content": "answer"}, "usage": "invalid"},
            {"model": {"unexpected": True}, "usage": {"prompt_tokens": "unknown", "completion_tokens": -1}},
        ]
        self.write_records("model.responses.log", [self.record(index + 1, status=500, body=body)
                                                   for index, body in enumerate(bodies)])
        detail = read_run(self.project, self.job)
        self.assertIsNotNone(detail)
        self.assertEqual([event["body"] for event in detail["events"]], bodies)
        self.assertTrue(all(event["isError"] for event in detail["events"]))
        self.assertTrue(all(event["usage"] == {"inputTokens": 0, "outputTokens": 0} for event in detail["events"]))
        self.assertEqual(detail["run"]["model"], "")

    def test_needs_input_and_running_jobs_have_distinct_statuses(self):
        self.lifecycle(end="needs_input")
        self.write_records("model.requests.log", [self.record(2, job_id=self.other_job, body={"model": "pending"})])
        self.assertEqual(list_runs(self.project, status="needs_input")["runs"][0]["runId"], self.job)
        self.assertEqual(list_runs(self.project, status="running")["runs"][0]["runId"], self.other_job)

    def test_generation_api_supports_task_filter_and_local_access_only(self):
        from fastapi.testclient import TestClient
        from service.api.server import create_app
        self.lifecycle()
        self.write_records("errors.log", [self.record(1, job_id=self.other_job, message="other")])
        with patch("service.api.server.Path.cwd", return_value=self.project), TestClient(
            create_app(Path("mcp_server.py"), "test"), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)
        ) as client:
            response = client.get(f"/api/logs/runs?job_id={self.job}")
            self.assertEqual(response.json()["page_size"], 20)
            self.assertEqual(client.get("/api/logs/runs?page_size=101").status_code, 422)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r["runId"] for r in response.json()["runs"]], [self.job])
            self.assertEqual(client.get(f"/api/logs/runs/{self.job}").json()["events"][-1]["status"], "failed")
            self.assertEqual(client.get("/api/logs/runs?job_id=..%2Fsecret").status_code, 400)
            self.assertEqual(client.get(f"/api/logs/runs?job_id={self.job}", headers={"Origin": "https://example.com"}).status_code, 403)

    def test_generation_reader_does_not_follow_log_symlinks(self):
        outside = Path(self.temp.name) / "outside.log"
        outside.write_text(json.dumps(self.record(1, message="private")), encoding="utf-8")
        try:
            (self.logs / "errors.log").symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks unavailable on this Windows installation")
        self.assertEqual(list_runs(self.project)["runs"], [])
