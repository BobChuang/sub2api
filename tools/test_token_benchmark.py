"""Offline regression tests; no test may connect to a network or call a model."""

from __future__ import annotations

import contextlib
import copy
import csv
import io
import itertools
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import token_benchmark as bench


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type="text/event-stream", clock=None):
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Type": content_type, "x-request-id": "request-test"}
        self.clock = clock

    def readline(self, size=-1):
        line = super().readline(size)
        if self.clock is not None and line.startswith(b"data:"):
            self.clock[0] += 1
        return line


def event_bytes(*events, done=True):
    text = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
    if done:
        text += "data: [DONE]\n\n"
    return text.encode("utf-8")


def chunk(delta=None, finish=None, **extra):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}


def sample_row(**overrides):
    row = {
        "sequence": 1, "round": 1, "channel": "cline", "model": bench.DEFAULT_MODELS[0],
        "wire_model": bench.DEFAULT_MODELS[0], "case_id": "arithmetic", "category": "test",
        "ok": True, "http_status": 200, "finish_reason": "stop", "error": None,
        "started_at": "2026-11-09T10:00:00+08:00", "headers_s": 0.5,
        "first_token_s": 1.0, "first_content_s": 2.0, "first_reasoning_s": 1.0,
        "total_s": 4.0, "generation_s": 2.0, "returned_model": "returned-model",
        "response_id": "response-test", "request_id": "request-test", "done_received": True,
        "content": "{}", "reasoning": "", "events": 4, "usage_raw": {},
        "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30,
        "cached_tokens": None, "reasoning_tokens": None, "cache_miss_tokens": None,
        "e2e_tokens_per_s": 2.5, "approx_generation_tokens_per_s": 5.0,
        "output_chars": 20, "output_chars_per_s": 5.0,
        "grade": {"score": 1.0, "passed": True, "details": {}},
    }
    row.update(overrides)
    return row


class OfflineTests(unittest.TestCase):
    def setUp(self):
        # Guard the real HTTP transport, including tests of main(--run). Only
        # explicit test doubles below can return responses.
        transport = mock.patch("urllib.request.OpenerDirector.open",
                               side_effect=AssertionError("network forbidden in offline tests"))
        self.blocked_transport = transport.start()
        self.addCleanup(transport.stop)
        sockets = mock.patch("socket.create_connection",
                             side_effect=AssertionError("network forbidden in offline tests"))
        sockets.start()
        self.addCleanup(sockets.stop)
        subprocesses = mock.patch("subprocess.run",
                                  side_effect=AssertionError("Docker forbidden in CLI unit tests"))
        self.blocked_subprocess = subprocesses.start()
        self.addCleanup(subprocesses.stop)

    def tearDown(self):
        self.blocked_transport.assert_not_called()
        self.blocked_subprocess.assert_not_called()

    def collect(self, body, content_type="text/event-stream"):
        client = bench.Client("http://localhost:8080/v1", 1000, [])
        clock = [0.0]
        ticks = itertools.count()
        with mock.patch.object(client, "open", return_value=FakeResponse(body, content_type, clock)), \
                mock.patch.object(bench.time, "perf_counter", side_effect=lambda: clock[0] + next(ticks) * 0.000001), \
                mock.patch.object(bench.threading, "Timer"):
            return bench.collect_stream(client, "offline-key", {"model": "test"})

    def test_sse_multiline_crlf_comments_and_eof(self):
        body = b': ping\r\nevent: message\r\ndata: {"x":\r\ndata: 1}\r\n\r\ndata:tail'
        self.assertEqual(list(bench.sse_events(io.BytesIO(body))), ['{"x":\n1}', "tail"])

    def test_reasoning_content_ttft_and_cumulative_usage(self):
        result = self.collect(event_bytes(
            chunk({"role": "assistant"}, id="response-1", model="model-1"),
            chunk({"reasoning_content": "先思考"}),
            chunk({"content": '{"answer":'}),
            chunk({"content": "1}"}),
            chunk(finish="stop", usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
        ))
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["first_token_s"], 2, places=4)
        self.assertAlmostEqual(result["first_reasoning_s"], 2, places=4)
        self.assertAlmostEqual(result["first_content_s"], 3, places=4)
        self.assertAlmostEqual(result["generation_s"], 2, places=4)
        self.assertEqual(result["content"], '{"answer":1}')
        self.assertEqual(result["reasoning"], "先思考")
        self.assertEqual(result["prompt_tokens"], 10)
        self.assertEqual(result["completion_tokens"], 4)
        self.assertEqual(result["total_tokens"], 14)
        self.assertTrue(result["done_received"])
        self.assertEqual(result["returned_model"], "model-1")
        self.assertEqual(result["request_id"], "request-test")

    def test_metadata_does_not_count_as_first_token(self):
        result = self.collect(event_bytes(chunk({"role": "assistant"}), chunk(finish="stop")))
        self.assertFalse(result["ok"])
        self.assertIsNone(result["first_token_s"])
        self.assertIsNone(result["first_content_s"])
        self.assertIsNone(result["first_reasoning_s"])

    def test_partial_stream_is_failure_but_preserves_content_and_usage(self):
        result = self.collect(event_bytes(
            chunk({"content": "partial"}, usage={"prompt_tokens": 11, "completion_tokens": 2}),
            done=False,
        ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["content"], "partial")
        self.assertEqual(result["completion_tokens"], 2)
        self.assertIn("截断", result["error"])
        self.assertFalse(result["done_received"])

    def test_error_event_is_failure_and_preserves_partial_response(self):
        result = self.collect(event_bytes(chunk({"content": "partial"}),
                                          {"error": {"message": "upstream failed"}}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["content"], "partial")
        self.assertIn("upstream failed", result["error"])
        self.assertIsNone(result["completion_tokens"])

    def test_finish_reason_without_done_is_still_a_truncated_stream(self):
        result = self.collect(event_bytes(chunk({"content": "complete"}), chunk(finish="stop"), done=False))
        self.assertFalse(result["ok"])
        self.assertIn("[DONE]", result["error"])

    def test_heartbeat_only_stream_obeys_deadline(self):
        with mock.patch.object(bench.time, "perf_counter", side_effect=[0.0, 0.5, 1.1]):
            with self.assertRaises(TimeoutError):
                list(bench.sse_events(io.BytesIO(b": heartbeat\n" * 8), deadline=1.0))

    def test_non_sse_response_is_rejected(self):
        result = self.collect(b'{"answer":1}', "application/json")
        self.assertFalse(result["ok"])
        self.assertIn("Content-Type", result["error"])

    def test_token_usage_zero_precedence_and_unknown_values(self):
        usage = bench.token_usage({
            "prompt_tokens": 0, "input_tokens": 999,
            "completion_tokens": 0, "output_tokens": 888,
            "prompt_tokens_details": {"cached_tokens": 0},
            "input_tokens_details": {"cached_tokens": 777},
            "prompt_cache_hit_tokens": 666,
            "completion_tokens_details": {"reasoning_tokens": 0}, "reasoning_tokens": 555,
        })
        self.assertEqual(usage["prompt_tokens"], 0)
        self.assertEqual(usage["completion_tokens"], 0)
        self.assertEqual(usage["cached_tokens"], 0)
        self.assertEqual(usage["reasoning_tokens"], 0)
        self.assertIsNone(usage["total_tokens"])
        self.assertTrue(all(value is None for value in bench.token_usage({}).values()))
        self.assertIsNone(bench.token_usage({"prompt_tokens": True})["prompt_tokens"])
        self.assertIsNone(bench.token_usage({"prompt_tokens_details": []})["cached_tokens"])

    def test_summarize_includes_failure_zero_and_partial_usage(self):
        rows = [sample_row(), sample_row(ok=False, grade={"score": 0.0, "passed": False},
                                        prompt_tokens=None, completion_tokens=3, total_tokens=None,
                                        first_token_s=None, first_content_s=None, total_s=100)]
        result = bench.summarize(rows)[0]
        self.assertEqual(result["quality_score"], 50)
        self.assertEqual(result["successful_quality_score"], 100)
        self.assertEqual(result["success_rate"], 0.5)
        self.assertEqual(result["completion_tokens"], 13)
        self.assertEqual(result["prompt_tokens_known_samples"], 1)
        self.assertEqual(result["usage_missing"], 1)
        self.assertEqual(result["total_s_p50"], 4)
        self.assertEqual(result["e2e_tokens_per_s"], 2.5)

    def test_summarize_distinguishes_missing_tokens_from_known_zero(self):
        missing = sample_row(prompt_tokens=None, completion_tokens=None, total_tokens=None)
        zero = sample_row(channel="opencode", prompt_tokens=0, completion_tokens=0, total_tokens=0)
        results = {row["channel"]: row for row in bench.summarize([missing, zero])}
        self.assertIsNone(results["cline"]["completion_tokens"])
        self.assertEqual(results["cline"]["completion_tokens_known_samples"], 0)
        self.assertIsNone(results["cline"]["by_case"]["arithmetic"]["completion_tokens"])
        self.assertEqual(results["opencode"]["completion_tokens"], 0)
        self.assertEqual(results["opencode"]["completion_tokens_known_samples"], 1)
        self.assertEqual(results["opencode"]["by_case"]["arithmetic"]["completion_tokens"], 0)

    def test_pairing_matches_exact_model_round_and_case(self):
        rows = [sample_row(round=1, total_s=4, completion_tokens=10),
                sample_row(round=2, case_id="extraction", total_s=6, completion_tokens=20),
                sample_row(channel="opencode", round=1, total_s=2, completion_tokens=5,
                           grade={"score": 0.5, "passed": False}),
                sample_row(channel="opencode", round=9, total_s=100),
                sample_row(channel="opencode", round=2, case_id="different", total_s=100),
                sample_row(channel="opencode", model=bench.DEFAULT_MODELS[1], total_s=100)]
        results = bench.paired_comparisons(rows)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["pairs"], 1)
        self.assertEqual(results[0]["both_success_samples"], 1)
        self.assertEqual(results[0]["quality_difference_points"], -50)
        self.assertEqual(results[0]["total_time_ratio_p50"], 0.5)
        self.assertEqual(results[0]["completion_token_ratio_p50"], 0.5)

    def test_plan_reuses_identical_prompts_for_all_six_pairs(self):
        mappings = {"opencode": {bench.DEFAULT_MODELS[0]: "explicit-flash-alias"}}
        plan = bench.build_plan(2, 73, mappings, None)
        self.assertEqual(plan, bench.build_plan(2, 73, mappings, None))
        self.assertEqual(len(plan), 2 * 8 * 6)
        groups = {}
        for step in plan:
            groups.setdefault((step["round"], step["case"]["id"]), []).append(step)
        for steps in groups.values():
            self.assertEqual(len(steps), 6)
            self.assertEqual(len({(step["channel"], step["model"]) for step in steps}), 6)
            self.assertTrue(all(step["case"] == steps[0]["case"] for step in steps))
        self.assertTrue(all(step["wire_model"] == "explicit-flash-alias" for step in plan
                            if step["channel"] == "opencode" and step["model"] == bench.DEFAULT_MODELS[0]))
        first = groups[(1, "arithmetic")][0]
        second = groups[(2, "arithmetic")][0]
        self.assertNotEqual(first["case"]["prompt"], second["case"]["prompt"])

    def test_default_main_is_network_blocked_dry_run(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(bench, "Client", side_effect=AssertionError("dry run created client")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench.main([]), 0)

    def test_system_keys_dry_run_does_not_read_docker_or_keys(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(bench, "load_system_keys", side_effect=AssertionError("dry run read keys")) as loader, \
                mock.patch.object(bench, "Client", side_effect=AssertionError("dry run created client")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench.main(["--system-keys"]), 0)
        loader.assert_not_called()

    def test_check_system_keys_only_requests_safe_metadata(self):
        metadata = {"source": "system_database_readonly", "identities": {
            "cline": {"name": "Cline", "group_name": "Test", "masked_key": "sk-fcf...cafd"},
            "opencode": {"name": "OpenCode", "group_name": "OpenCode", "masked_key": "sk-d01...6c97"},
            "commandcode": {"name": "commandcode", "group_name": "cc", "masked_key": "sk-c58...bbeb"},
        }}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(bench, "load_system_keys", return_value=({}, metadata)) as loader, \
                mock.patch.object(bench, "Client", side_effect=AssertionError("metadata check created API client")), \
                mock.patch.object(bench, "build_plan", side_effect=AssertionError("metadata check built model plan")), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(bench.main(["--check-system-keys", "--service-container", "service-test",
                                         "--database-container", "postgres-test"]), 0)
        loader.assert_called_once_with(bench.DEFAULT_BASE_URL, "service-test", "postgres-test", include_secrets=False)
        self.assertIn("Cline", stdout.getvalue())
        self.assertIn("sk-fcf...cafd", stdout.getvalue())

    def test_conflicting_system_key_flags_are_rejected_before_loading(self):
        options = (["--system-keys", "--env-file", "does-not-exist.env"],
                   ["--check-system-keys", "--env-file", "does-not-exist.env"],
                   ["--check-system-keys", "--run"])
        for argv in options:
            with self.subTest(argv=argv), \
                    mock.patch.object(bench, "load_system_keys", side_effect=AssertionError("conflicting flags read keys")) as loader, \
                    mock.patch.object(bench, "load_env", side_effect=AssertionError("conflicting flags loaded env")), \
                    self.assertRaisesRegex(ValueError, "env-file|--run"):
                bench.main(argv)
            loader.assert_not_called()

    def test_run_without_credentials_stops_before_creating_client(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(bench, "Client", side_effect=AssertionError("missing keys created client")), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "CLINE_API_KEY"):
            bench.main(["--run"])

    def test_missing_target_model_stops_before_any_completion(self):
        keys = {name: f"offline-key-{channel}" for channel, name in bench.KEY_ENVS.items()}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with mock.patch.dict(os.environ, keys, clear=True), \
                    mock.patch.object(bench.Client, "get", return_value={"data": [{"id": bench.DEFAULT_MODELS[0]}]}) as get, \
                    mock.patch.object(bench, "collect_stream", side_effect=AssertionError("preflight sent completion")) as collect, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = bench.main(["--run", "--rounds", "1", "--cases", "arithmetic", "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertEqual(get.call_count, 3)
            self.assertTrue(all(call.args[0] == "/models" for call in get.call_args_list))
            collect.assert_not_called()
            self.assertIn("error", json.loads((output / "preflight_error.json").read_text()))
            self.assertFalse((output / "results.jsonl").exists())

    def test_billing_model_delta_requires_available_model_baselines(self):
        before = {"total": {"requests": 10, "actual_cost": 1.0}, "model_stats": None}
        after = {"total": {"requests": 11, "actual_cost": 1.1},
                 "model_stats": [{"model": "m", "requests": 500, "actual_cost": 50.0}]}
        result = bench.billing_delta(before, after, [sample_row()], "cline")
        self.assertEqual(result["model_deltas"], [])
        self.assertFalse(result["model_baseline_known"])
        self.assertAlmostEqual(result["counter_delta"]["actual_cost"], 0.1)

    def test_empty_account_counter_is_a_valid_zero_model_baseline(self):
        before = {"total": {"requests": 0, "actual_cost": 0.0}, "model_stats": None}
        after = {"total": {"requests": 1, "actual_cost": 0.1},
                 "model_stats": [{"model": "m", "requests": 1, "actual_cost": 0.1}]}
        result = bench.billing_delta(before, after, [sample_row()], "cline")
        self.assertTrue(result["model_baseline_known"])
        self.assertEqual(result["model_deltas"][0]["requests"], 1)
        self.assertAlmostEqual(result["model_deltas"][0]["actual_cost"], 0.1)

    def mocked_full_run(self, directory, fail_first=False, system_keys=False):
        keys = {name: f"offline-key-{channel}" for channel, name in bench.KEY_ENVS.items()}
        usage_calls = {key: 0 for key in keys.values()}
        payloads = []
        case = next(case for case in bench.get_cases(0, 20261109) if case["id"] == "arithmetic")

        def fake_get(client, path, key):
            if path == "/models":
                return {"data": [{"id": model} for model in bench.DEFAULT_MODELS]}
            self.assertTrue(path.startswith("/usage?"))
            self.assertIn("start_date=", path)
            usage_calls[key] += 1
            after = usage_calls[key] > 1
            total = {"requests": 10, "input_tokens": 1000, "output_tokens": 500,
                     "total_tokens": 1500, "cost": 1.0, "actual_cost": 0.5}
            model_stats = [{"model": model, "requests": 5, "cost": 0.5, "actual_cost": 0.25}
                           for model in bench.DEFAULT_MODELS]
            if after:
                for name, increment in {"requests": 2, "input_tokens": 20, "output_tokens": 8,
                                        "total_tokens": 28, "cost": 0.04, "actual_cost": 0.02}.items():
                    total[name] += increment
                for stats in model_stats:
                    stats["requests"] += 1
                    stats["cost"] += 0.02
                    stats["actual_cost"] += 0.01
            return {"usage": {"total": total}, "model_stats": model_stats}

        def fake_collect(client, key, payload):
            payloads.append(copy.deepcopy(payload))
            failed = fail_first and len(payloads) == 1
            return sample_row(ok=not failed, finish_reason=None if failed else "stop",
                              error="offline simulated reset" if failed else None,
                              returned_model=payload["model"],
                              content=json.dumps(case["expected"], ensure_ascii=False),
                              prompt_tokens=10, completion_tokens=4, total_tokens=14)

        output = Path(directory) / "run"
        system_key_values = {channel: keys[name] for channel, name in bench.KEY_ENVS.items()}
        metadata = {"source": "system_database_readonly", "identities": {
            channel: {"name": channel, "group_name": "test", "masked_key": "sk-abc...def0"}
            for channel in bench.CHANNELS}}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {} if system_keys else keys, clear=True), \
                mock.patch.object(bench, "load_system_keys", return_value=(system_key_values, metadata)) as loader, \
                mock.patch.object(bench.Client, "get", autospec=True, side_effect=fake_get), \
                mock.patch.object(bench, "collect_stream", side_effect=fake_collect), \
                mock.patch.object(bench.time, "sleep"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            argv = ["--run", "--rounds", "1", "--cases", "arithmetic", "--pause", "0",
                    "--billing-wait", "0", "--output", str(output)]
            if system_keys:
                argv.append("--system-keys")
            code = bench.main(argv)
        if system_keys:
            loader.assert_called_once_with(bench.DEFAULT_BASE_URL, "sub2api", "sub2api-postgres", include_secrets=True)
        else:
            loader.assert_not_called()
        self.assertTrue(all(key not in stdout.getvalue() + stderr.getvalue() for key in keys.values()))
        return code, output, payloads, keys

    def test_system_keys_full_run_loads_secrets_only_for_run_and_saves_safe_source(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output, payloads, keys = self.mocked_full_run(directory, system_keys=True)
            self.assertEqual(code, 0)
            self.assertEqual(len(payloads), 6)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["key_source"]["source"], "system_database_readonly")
            self.assertEqual(set(manifest["key_source"]["identities"]), set(bench.CHANNELS))
            serialized = "\n".join(path.read_text(encoding="utf-8-sig") for path in output.iterdir())
            self.assertTrue(all(key not in serialized for key in keys.values()))

    def test_mocked_full_run_writes_reports_and_reconciles_real_counter_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output, payloads, keys = self.mocked_full_run(directory)
            self.assertEqual(code, 0)
            self.assertEqual(len(payloads), 6)
            self.assertTrue(all(payload["messages"] == payloads[0]["messages"] for payload in payloads))
            self.assertTrue(all(payload["temperature"] == 0 and payload["stream"] for payload in payloads))
            self.assertTrue(all(payload["max_tokens"] == 4096 for payload in payloads))
            for name in ("manifest.json", "models.json", "billing_before.json", "billing_after.json", "summary.json"):
                self.assertIsInstance(json.loads((output / name).read_text()), dict)
            rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(row["grade"]["score"] == 1.0 for row in rows))
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["completed_attempts"], 6)
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(len(summary["groups"]), 6)
            self.assertEqual(json.loads((output / "manifest.json").read_text())["max_output_tokens_sum"], 6 * 4096)
            for billing in summary["billing"]:
                self.assertEqual(billing["counter_delta"]["requests"], 2)
                self.assertEqual(billing["counter_delta"]["input_tokens"], 20)
                self.assertEqual(billing["counter_delta"]["output_tokens"], 8)
                self.assertAlmostEqual(billing["counter_delta"]["actual_cost"], 0.02)
                self.assertTrue(billing["request_count_matched"])
                self.assertEqual(len(billing["model_deltas"]), 2)
                for model in billing["model_deltas"]:
                    self.assertEqual(model["requests"], 1)
                    self.assertAlmostEqual(model["actual_cost"], 0.01)
            with (output / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 6)
            self.assertIn("实际扣费", (output / "report.md").read_text())
            serialized = "\n".join(path.read_text(encoding="utf-8-sig") for path in output.iterdir())
            self.assertTrue(all(key not in serialized for key in keys.values()))

    def test_failed_call_receives_zero_grade_despite_correct_partial_content(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output, _, _ = self.mocked_full_run(directory, fail_first=True)
            self.assertEqual(code, 1)
            rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
            self.assertFalse(rows[0]["ok"])
            self.assertEqual(rows[0]["grade"]["score"], 0)
            self.assertEqual(rows[1]["grade"]["score"], 1)
            summary = json.loads((output / "summary.json").read_text())
            group = next(group for group in summary["groups"] if
                         (group["channel"], group["model"]) == (rows[0]["channel"], rows[0]["model"]))
            self.assertEqual(group["quality_score"], 0)
            self.assertEqual(group["success_rate"], 0)
            billing = next(item for item in summary["billing"] if item["channel"] == rows[0]["channel"])
            self.assertFalse(billing["request_count_matched"])


if __name__ == "__main__":
    unittest.main()
