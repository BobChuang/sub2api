#!/usr/bin/env python3
"""Repeatable, opt-in channel benchmark. Defaults to an entirely offline plan."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from token_benchmark_cases import get_cases, score_case
from token_benchmark_system_keys import load_system_keys


CHANNELS = ("cline", "opencode", "commandcode")
KEY_ENVS = {name: name.upper() + "_API_KEY" for name in CHANNELS}
DEFAULT_MODELS = ("deepseek-v4.1-flash", "deepseek-v4-pro")
DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]
BILLING_FIELDS = ("requests", "input_tokens", "output_tokens", "cache_creation_tokens",
                  "cache_read_tokens", "total_tokens", "cost", "actual_cost")


def now() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="milliseconds")


def load_env(path: Path | None) -> None:
    """Load literal KEY=value entries, never execute or interpolate shell code."""
    if path is None:
        return
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"{path.name}:{line_no}: 需要 KEY=value")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ValueError(f"{path.name}:{line_no}: 环境变量名不合法")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def normalize_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Base URL 必须是 http(s) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL 不应含账号、密码、查询参数或片段")
    if not parsed.path:
        value += "/v1"
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Redirects could hide route mistakes and change the benchmark's network path.
        return None


class Client:
    def __init__(self, base_url: str, timeout: float, secrets: list[str]):
        self.base_url = base_url
        self.timeout = timeout
        self.secrets = secrets
        # In particular, do not send localhost through HTTP(S)_PROXY.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def clean(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)

    def open(self, path: str, key: str, payload: dict | None = None):
        headers = {"Authorization": "Bearer " + key,
                   "User-Agent": "sub2api-token-benchmark/1.0",
                   "Accept": "text/event-stream" if payload else "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        return self.opener.open(request, timeout=self.timeout)

    def get(self, path: str, key: str) -> dict:
        with self.open(path, key) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("API 返回值不是 JSON 对象")
        return data

    def error(self, exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            body = exc.read(8192).decode("utf-8", errors="replace")
            return self.clean(f"HTTP {exc.code}: {body}")
        return self.clean(f"{type(exc).__name__}: {exc}")


def sse_events(response, deadline: float | None = None):
    """Read complete SSE events (including multiline data and EOF without blank line)."""
    parts = []
    size = 0
    while True:
        if deadline is not None and time.perf_counter() >= deadline:
            raise TimeoutError("流式读取超过总时限")
        raw = response.readline(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("SSE 单行超过 2 MiB")
        if not raw:
            if parts:
                yield "\n".join(parts)
            return
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if parts:
                yield "\n".join(parts)
            parts, size = [], 0
        elif line.startswith("data:"):
            value = line[5:]
            value = value[1:] if value.startswith(" ") else value
            parts.append(value)
            size += len(raw)
            if size > 4 * 1024 * 1024:
                raise ValueError("SSE 单事件超过 4 MiB")


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def token_usage(raw: dict) -> dict:
    def first(*values):
        return next((v for v in values if numeric(v) and v >= 0), None)

    def detail(field, key):
        value = raw.get(field)
        return value.get(key) if isinstance(value, dict) else None

    prompt = first(raw.get("prompt_tokens"), raw.get("input_tokens"))
    output = first(raw.get("completion_tokens"), raw.get("output_tokens"))
    cached = first(detail("prompt_tokens_details", "cached_tokens"),
                   detail("input_tokens_details", "cached_tokens"),
                   raw.get("prompt_cache_hit_tokens"), raw.get("cache_read_input_tokens"))
    reasoning = first(detail("completion_tokens_details", "reasoning_tokens"),
                      detail("output_tokens_details", "reasoning_tokens"),
                      raw.get("reasoning_tokens"))
    return {"prompt_tokens": prompt, "completion_tokens": output,
            "total_tokens": first(raw.get("total_tokens")),
            "cached_tokens": cached, "reasoning_tokens": reasoning,
            "cache_miss_tokens": first(raw.get("prompt_cache_miss_tokens"))}


def collect_stream(client: Client, key: str, payload: dict) -> dict:
    started = time.perf_counter()
    row = {"started_at": now(), "ok": False, "http_status": None,
           "headers_s": None, "first_token_s": None, "first_content_s": None,
           "first_reasoning_s": None, "total_s": None, "generation_s": None,
           "returned_model": None, "response_id": None, "request_id": None,
           "finish_reason": None, "done_received": False, "content": "",
           "reasoning": "", "usage_raw": {}, "events": 0, "error": None}
    contents, reasonings = [], []
    last_generation_s = None
    watchdog = None
    expired = threading.Event()
    try:
        with client.open("/chat/completions", key, payload) as response:
            row["http_status"] = response.status
            row["headers_s"] = time.perf_counter() - started
            row["request_id"] = response.headers.get("x-request-id") or response.headers.get("request-id")
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type.lower():
                raise ValueError(f"预期 SSE 流，收到 Content-Type={content_type!r}")
            remaining = client.timeout - (time.perf_counter() - started)
            if remaining <= 0:
                raise TimeoutError("连接阶段已超过请求时限")
            # A socket inactivity timeout alone cannot stop endless heartbeat or
            # trickle-byte streams. Shutdown wakes a read blocked inside readline.
            stream_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)

            def abort_stream():
                expired.set()
                if stream_socket is not None:
                    try:
                        stream_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            watchdog = threading.Timer(remaining, abort_stream)
            watchdog.daemon = True
            watchdog.start()
            for data in sse_events(response, started + client.timeout):
                elapsed = time.perf_counter() - started
                if elapsed > client.timeout:
                    raise TimeoutError("请求超过总超时限制")
                if data.strip() == "[DONE]":
                    row["done_received"] = True
                    break
                event = json.loads(data)
                if not isinstance(event, dict):
                    raise ValueError("SSE 数据不是 JSON 对象")
                row["events"] += 1
                if event.get("error"):
                    raise ValueError("流式错误: " + json.dumps(event["error"], ensure_ascii=False))
                row["returned_model"] = event.get("model") or row["returned_model"]
                row["response_id"] = event.get("id") or row["response_id"]
                if isinstance(event.get("usage"), dict) and event["usage"]:
                    # Providers may repeat a cumulative object; never sum chunks.
                    row["usage_raw"] = event["usage"]
                for choice in event.get("choices") or []:
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason") is not None:
                        row["finish_reason"] = choice["finish_reason"]
                    text = delta.get("content") or ""
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if not isinstance(text, str) or not isinstance(reasoning, str):
                        raise ValueError("当前脚本只支持字符串正文与推理字段")
                    if text or reasoning:
                        if row["first_token_s"] is None:
                            row["first_token_s"] = elapsed
                        last_generation_s = elapsed
                    if text:
                        if row["first_content_s"] is None:
                            row["first_content_s"] = elapsed
                        contents.append(text)
                    if reasoning:
                        if row["first_reasoning_s"] is None:
                            row["first_reasoning_s"] = elapsed
                        reasonings.append(reasoning)
            if expired.is_set():
                raise TimeoutError("流式读取超过总时限")
            if not row["done_received"]:
                raise ValueError("流被截断：没有 [DONE] 结束标记")
            if row["finish_reason"] is None:
                raise ValueError("流被截断：没有 finish_reason")
            if row["finish_reason"] not in ("stop", "length"):
                raise ValueError(f"非正常结束: {row['finish_reason']}")
            if not contents:
                raise ValueError("响应没有正文")
            row["ok"] = True
    except Exception as exc:
        row["error"] = client.error(exc)
        if isinstance(exc, urllib.error.HTTPError):
            row["http_status"] = exc.code
    finally:
        if watchdog is not None:
            watchdog.cancel()
        row["total_s"] = time.perf_counter() - started
        row["content"] = "".join(contents)
        row["reasoning"] = "".join(reasonings)
        if last_generation_s is not None and row["first_token_s"] is not None:
            row["generation_s"] = max(0, last_generation_s - row["first_token_s"])
        row.update(token_usage(row["usage_raw"]))
        row["output_chars"] = len(row["content"])
        count = row["completion_tokens"]
        row["e2e_tokens_per_s"] = count / row["total_s"] if count is not None else None
        row["approx_generation_tokens_per_s"] = (
            count / row["generation_s"] if count is not None and row["generation_s"] else None)
        row["output_chars_per_s"] = row["output_chars"] / row["total_s"]
    return row


def build_plan(rounds: int, seed: int, mappings: dict, selected: list[str] | None) -> list[dict]:
    rng = random.Random(seed)
    plan = []
    pairs = [(channel, model) for channel in CHANNELS for model in DEFAULT_MODELS]
    rng.shuffle(pairs)
    for round_index in range(rounds):
        cases = get_cases(round_index, seed)
        if selected:
            unknown = set(selected) - {case["id"] for case in cases}
            if unknown:
                raise ValueError("未知题目 ID: " + ", ".join(sorted(unknown)))
            cases = [case for case in cases if case["id"] in selected]
        rng.shuffle(cases)
        for case_index, case in enumerate(cases):
            # Rotate all six pairs to balance first/last positions across matched tasks.
            offset = (round_index + case_index) % len(pairs)
            order = pairs[offset:] + pairs[:offset]
            if round_index % 2:
                order = list(reversed(order))
            for channel, model in order:
                plan.append({"sequence": len(plan) + 1, "round": round_index + 1,
                             "channel": channel, "model": model,
                             "wire_model": mappings.get(channel, {}).get(model, model), "case": case})
    return plan


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def percentile(values: list[float], fraction: float):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * fraction
    low = int(pos)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (pos - low)


def summarize(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["channel"], row["model"])].append(row)
    summaries = []
    for (channel, model), batch in sorted(groups.items()):
        good = [r for r in batch if r["ok"]]
        score = statistics.mean(r["grade"]["score"] for r in batch)
        item = {"channel": channel, "model": model, "attempts": len(batch),
                "successes": len(good), "success_rate": len(good) / len(batch),
                "quality_score": score * 100,
                "successful_quality_score": statistics.mean(r["grade"]["score"] for r in good) * 100 if good else None,
                "pass_rate": sum(r["grade"]["passed"] for r in batch) / len(batch),
                "truncated": sum(r["finish_reason"] == "length" for r in batch),
                "usage_missing": sum(r["prompt_tokens"] is None or r["completion_tokens"] is None for r in batch),
                "returned_models": sorted({r["returned_model"] for r in batch if r["returned_model"]})}
        for metric in ("first_token_s", "first_content_s", "total_s"):
            values = [r[metric] for r in good if r[metric] is not None]
            item[metric + "_p50"] = percentile(values, 0.5)
            item[metric + "_p95"] = percentile(values, 0.95)
        # Totals always include partial usage on failures; unknown counts stay unknown.
        for metric in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
            known = [r[metric] for r in batch if r[metric] is not None]
            item[metric] = sum(known) if known else None
            item[metric + "_known_samples"] = len(known)
        measured = [r for r in good if r["completion_tokens"] is not None]
        item["e2e_tokens_per_s"] = (sum(r["completion_tokens"] for r in measured) /
                                    sum(r["total_s"] for r in measured) if measured else None)
        item["output_chars_per_s"] = (sum(r["output_chars"] for r in good) /
                                      sum(r["total_s"] for r in good) if good else None)
        item["by_case"] = {}
        for case_id in sorted({r["case_id"] for r in batch}):
            subset = [r for r in batch if r["case_id"] == case_id]
            known_outputs = [r["completion_tokens"] for r in subset if r["completion_tokens"] is not None]
            item["by_case"][case_id] = {
                "attempts": len(subset), "quality_score": statistics.mean(r["grade"]["score"] for r in subset) * 100,
                "total_s_p50": percentile([r["total_s"] for r in subset if r["ok"]], 0.5),
                "completion_tokens": sum(known_outputs) if known_outputs else None,
                "usage_known_samples": sum(r["completion_tokens"] is not None for r in subset)}
        summaries.append(item)
    return summaries


def paired_comparisons(rows: list[dict]) -> list[dict]:
    """Match on exact round+case, compare each channel to cline separately per model."""
    lookup = {(r["channel"], r["model"], r["round"], r["case_id"]): r for r in rows}
    comparisons = []
    for model in DEFAULT_MODELS:
        references = [r for r in rows if r["channel"] == "cline" and r["model"] == model]
        for channel in CHANNELS[1:]:
            pairs = [(a, lookup.get((channel, model, a["round"], a["case_id"]))) for a in references]
            pairs = [(a, b) for a, b in pairs if b is not None]
            if not pairs:
                continue
            latencies = [b["total_s"] / a["total_s"] for a, b in pairs if a["ok"] and b["ok"]]
            token_ratios = [b["completion_tokens"] / a["completion_tokens"] for a, b in pairs
                            if a["ok"] and b["ok"] and a["completion_tokens"] and b["completion_tokens"] is not None]
            comparisons.append({"model": model, "channel": channel, "reference": "cline", "pairs": len(pairs),
                                "quality_difference_points": statistics.mean(b["grade"]["score"] - a["grade"]["score"] for a, b in pairs) * 100,
                                "both_success_samples": len(latencies),
                                "total_time_ratio_p50": percentile(latencies, 0.5),
                                "completion_token_ratio_p50": percentile(token_ratios, 0.5)})
    return comparisons


def snapshot(client: Client, key: str, query: str) -> dict:
    result = {"captured_at": now(), "total": None, "model_stats": None, "error": None}
    try:
        data = client.get("/usage?" + query, key)
        # Save only counters: the response can also contain unrelated account balances.
        total = (data.get("usage") or {}).get("total")
        result["total"] = {k: total[k] for k in BILLING_FIELDS if k in total} if isinstance(total, dict) else None
        if isinstance(data.get("model_stats"), list):
            result["model_stats"] = [{k: item[k] for k in ("model",) + BILLING_FIELDS if k in item}
                                     for item in data["model_stats"] if isinstance(item, dict)]
        if result["total"] is None:
            result["error"] = "没有 usage.total 字段；实际费用未知"
    except Exception as exc:
        result["error"] = client.error(exc)
    return result


def counter_delta(before: dict | None, after: dict | None) -> dict:
    if before is None or after is None:
        return {}
    return {key: after[key] - before[key] for key in BILLING_FIELDS
            if numeric(before.get(key)) and numeric(after.get(key))}


def billing_delta(before: dict, after: dict, rows: list[dict], channel: str) -> dict:
    delta = counter_delta(before.get("total"), after.get("total"))
    attempts = [r for r in rows if r["channel"] == channel]
    expected = sum(r["ok"] for r in attempts)
    clean_run = bool(attempts) and all(r["ok"] for r in attempts)
    old = {m["model"]: m for m in before.get("model_stats") or [] if isinstance(m.get("model"), str)}
    new = {m["model"]: m for m in after.get("model_stats") or [] if isinstance(m.get("model"), str)}
    models = []
    # Omitted model_stats can mean a best-effort query failed, not zero history.
    model_baseline_known = isinstance(before.get("model_stats"), list) or (
        before.get("total") is not None and before["total"].get("requests") == 0)
    if model_baseline_known and isinstance(after.get("model_stats"), list):
        for model in sorted(set(old) | set(new)):
            changes = counter_delta(old.get(model, dict.fromkeys(BILLING_FIELDS, 0)),
                                    new.get(model, dict.fromkeys(BILLING_FIELDS, 0)))
            if any(value != 0 for value in changes.values()):
                models.append({"logged_model": model, **changes})
    valid = bool(delta) and all(value >= -1e-8 for value in delta.values())
    matched = valid and delta.get("requests") == expected and clean_run
    return {"channel": channel, "attempts": len(attempts), "successful_responses": expected,
            "counter_delta": delta, "model_deltas": models, "request_count_matched": matched,
            "model_baseline_known": model_baseline_known,
            "attribution": "request_count_matched_exclusive_key_assumption" if matched else "unreconciled",
            "note": "费用为快照窗口差额；需该 Key 在实验期间无其他调用。请求数吻合也不能排除并发/延迟入账恰好抵消。"}


def fmt(value, digits=2) -> str:
    return "未知" if value is None else f"{value:.{digits}f}"


def save_report(out: Path, manifest: dict, rows: list[dict], billing: list[dict], status: str) -> None:
    groups = summarize(rows)
    paired = paired_comparisons(rows)
    summary = {"status": status, "generated_at": now(), "scheduled_requests": manifest["requests"],
               "completed_attempts": len(rows), "groups": groups, "paired_comparisons": paired,
               "billing": billing}
    write_json(out / "summary.json", summary)
    fields = ["sequence", "round", "channel", "model", "wire_model", "case_id", "ok", "http_status",
              "finish_reason", "total_s", "first_token_s", "first_content_s", "e2e_tokens_per_s",
              "prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens", "output_chars", "error"]
    with (out / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields + ["quality_score"], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "quality_score": row["grade"]["score"] * 100})
    lines = ["# 渠道 Token 质量试验", "", f"状态：{status}；已尝试 {len(rows)}/{manifest['requests']} 次。",
             f"时间：{now()}；地址：{manifest['base_url']}。", "",
             "## 同题对比", "", "质量分含失败=0；速度仅统计成功响应。token 合计只加已返回值，未知不当作零。",
             "p95 样本少时仅作描述；各题耗时差异可查 summary.json 的 by_case。", "",
             "| 渠道 | 模型 | 成功/尝试 | 质量/100 | 正文首字 p50/p95 秒 | 总耗时 p50/p95 秒 | 端到端 输出token/s | 输入/输出token | 缺失用量 | 截断 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for g in groups:
        lines.append(f"| {g['channel']} | {g['model']} | {g['successes']}/{g['attempts']} | {fmt(g['quality_score'])} | "
                     f"{fmt(g['first_content_s_p50'])}/{fmt(g['first_content_s_p95'])} | {fmt(g['total_s_p50'])}/{fmt(g['total_s_p95'])} | "
                     f"{fmt(g['e2e_tokens_per_s'])} | {g['prompt_tokens'] if g['prompt_tokens'] is not None else '未知'}/"
                     f"{g['completion_tokens'] if g['completion_tokens'] is not None else '未知'} | {g['usage_missing']} | {g['truncated']} |")
    lines += ["", "## 配对比较", "", "以下比例是同轮同题相对 cline 的中位数；<1 表示用时或输出 token 更少，不能单独说明质量更好。", "",
              "| 模型 | 渠道 | 配对数/均成功 | 质量差（百分点） | 用时比 | 输出token比 |", "|---|---|---:|---:|---:|---:|"]
    for p in paired:
        lines.append(f"| {p['model']} | {p['channel']} | {p['pairs']}/{p['both_success_samples']} | "
                     f"{fmt(p['quality_difference_points'])} | {fmt(p['total_time_ratio_p50'])} | {fmt(p['completion_token_ratio_p50'])} |")
    lines += ["", "## 网关实际扣费", "", "按当前 Key 的 /usage 前后快照计算；不是按官网单价估算。",
              "请求数不符、任何失败、计数回退或查不到数据时均标记未对账。即使数量吻合，也需要这些 Key 没有其他调用。", "",
              "| 渠道 | 快照请求增量/成功响应 | 标准费用差 USD | 实扣差 USD | 对账 |", "|---|---:|---:|---:|---|"]
    for b in billing:
        d = b["counter_delta"]
        lines.append(f"| {b['channel']} | {d.get('requests', '未知')}/{b['successful_responses']} | "
                     f"{fmt(d.get('cost'), 6)} | {fmt(d.get('actual_cost'), 6)} | {'数量吻合（需专用 Key）' if b['request_count_matched'] else '未对账，不能认定为实验费用'} |")
    lines += ["", "分模型快照差额（这里使用网关账单里的模型名，可能与请求别名不同）：", "",
              "| 渠道 | 账单模型 | 请求增量 | 实扣差 USD |", "|---|---|---:|---:|"]
    for b in billing:
        for m in b["model_deltas"]:
            lines.append(f"| {b['channel']} | {m['logged_model']} | {m.get('requests', '未知')} | {fmt(m.get('actual_cost'), 6)} |")
    lines += ["", "## 解释范围", "",
              "- 这是固定小题集的正确性与服务表现测试；不代表通用能力排行榜，不能据此证明上游模型身份或 token 是否造假。",
              "- 网关可能改写响应 model，保留请求与返回名称仅用于核查；未做隐藏模型替换或失败自动重试。",
              "- 三轮使用不同数据；同轮六组题目完全相同，因此仍可能跨渠道命中上游共享缓存。缓存命中率单独记录，不能声称是纯冷缓存。",
              "- 推理 token 与缓存 token 是服务端报告；缺失时保留未知。completion_tokens 通常包含推理，具体以渠道口径为准。",
              "- 正文首字从第一个非空 content 计时；first_token_s 包含推理片段。端到端吞吐为输出 token 总量/完整响应总耗时。",
              "- approx_generation_tokens_per_s 是分块间隔估值；首块大小与推理口径会影响它，不将它当精确解码速度。",
              "- 推理模式沿用各渠道默认配置，未统一 thinking/reasoning_effort；结果衡量当前渠道整体表现。不同默认推理设置可能造成差异。",
              "- 所有调用串行并轮换顺序；从本机直连也仍包含网关排队和上游网络耗时。未隔离上游缓存、共享负载和时段影响。",
              "- manifest.json 保存题目与执行参数；results.jsonl 保存逐次正文、推理、评分与原始 usage，可人工复核。", ""]
    if any(not r["ok"] for r in rows):
        lines += ["## 失败记录", ""]
        for r in rows:
            if not r["ok"]:
                safe_error = str(r["error"]).replace("\n", " ")
                lines.append(f"- #{r['sequence']} {r['channel']} / {r['model']} / {r['case_id']}: {safe_error[:600]}")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="三渠道 DeepSeek 质量/速度/用量对比；默认离线预览，--run 才访问 API")
    parser.add_argument("--env-file", type=Path, help="本地 KEY=value 文件；不执行文件内容")
    parser.add_argument("--system-keys", action="store_true", help="直接读取本机 Sub2API 已有的三个 Key，无需 .env")
    parser.add_argument("--check-system-keys", action="store_true", help="只读核对系统中的 Key 名称、分组和掩码；不调用模型")
    parser.add_argument("--service-container", default="sub2api", help="本机 Sub2API Docker 容器名")
    parser.add_argument("--database-container", default="sub2api-postgres", help="该服务的 PostgreSQL Docker 容器名")
    parser.add_argument("--base-url", help=f"默认 {DEFAULT_BASE_URL}，也可用 BENCH_BASE_URL")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261109)
    parser.add_argument("--max-tokens", type=int, default=4096, help="每题统一输出上限，含推理时以渠道口径为准（默认4096）")
    parser.add_argument("--cases", help="仅跑指定题目 ID，逗号分隔")
    parser.add_argument("--model-map", type=Path, help="各渠道精确模型 ID 的 JSON 文件")
    parser.add_argument("--timeout", type=float, default=180, help="连接空闲超时及流式总时限秒数（默认180）")
    parser.add_argument("--pause", type=float, default=0.5, help="请求之间间隔秒数")
    parser.add_argument("--billing-wait", type=float, default=10, help="结束后最多等待账单入库秒数")
    parser.add_argument("--output", type=Path, help="结果目录，必须是新目录")
    parser.add_argument("--run", action="store_true", help="明确发起真实 API 请求；有计费")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if (args.system_keys or args.check_system_keys) and args.env_file:
        raise ValueError("系统 Key 模式不与 --env-file 混用")
    if args.check_system_keys and args.run:
        raise ValueError("--check-system-keys 只做只读核对，不能与 --run 一起使用")
    load_env(args.env_file)
    if args.rounds < 1 or args.rounds > 100:
        raise ValueError("rounds 允许 1..100")
    if args.max_tokens < 256 or args.max_tokens > 65536:
        raise ValueError("max-tokens 允许 256..65536")
    if not all(math.isfinite(v) for v in (args.timeout, args.pause, args.billing_wait)) or args.timeout <= 0 or args.pause < 0 or args.billing_wait < 0:
        raise ValueError("timeout 必须为正数，pause/billing-wait 不能为负")
    base_url = normalize_url(args.base_url or os.getenv("BENCH_BASE_URL", DEFAULT_BASE_URL))
    if args.check_system_keys:
        _, metadata = load_system_keys(base_url, args.service_container, args.database_container, include_secrets=False)
        print("只读核对完成；未调用模型，未修改账号、分组或 Key。")
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        return 0
    mappings = json.loads(args.model_map.read_text(encoding="utf-8")) if args.model_map else {}
    if not isinstance(mappings, dict) or set(mappings) - set(CHANNELS):
        raise ValueError("model-map 必须是以 cline/opencode/commandcode 为键的对象")
    for channel, mapping in mappings.items():
        if not isinstance(mapping, dict) or set(mapping) - set(DEFAULT_MODELS) or any(not isinstance(v, str) or not v.strip() for v in mapping.values()):
            raise ValueError(f"{channel} 的 model-map 无效")
        if len({mapping.get(model, model) for model in DEFAULT_MODELS}) != 2:
            raise ValueError(f"{channel} 的两个模型不能映射到同一个 ID")
    selected = [s.strip() for s in args.cases.split(",") if s.strip()] if args.cases else None
    plan = build_plan(args.rounds, args.seed, mappings, selected)
    for step in plan:
        step["case"]["max_tokens"] = args.max_tokens
    output_cap = sum(step["case"]["max_tokens"] for step in plan)
    print(f"{'真实执行' if args.run else '离线预览（零网络请求）'}: {base_url}")
    print(f"{len(CHANNELS)} 渠道 × {len(DEFAULT_MODELS)} 模型 × {args.rounds} 轮 × {len(plan) // (6 * args.rounds)} 题 = {len(plan)} 次推理请求")
    print(f"max_tokens 合计上限: {output_cap:,}（不是预计用量或费用；上游口径可能不同）")
    print("首轮题目: " + ", ".join(dict.fromkeys(s["case"]["id"] for s in plan if s["round"] == 1)))
    for channel in CHANNELS:
        print(channel + ": " + ", ".join(mappings.get(channel, {}).get(model, model) for model in DEFAULT_MODELS))
    if not args.run:
        print("不会连接服务、检查 Key 或生成模型回答。确认后加 --run。")
        return 0
    if args.system_keys:
        keys, key_source = load_system_keys(base_url, args.service_container, args.database_container, include_secrets=True)
        for channel, identity in key_source["identities"].items():
            print(f"系统 Key: {identity['name']} / 分组 {identity['group_name']} / {identity['masked_key']}")
    else:
        keys = {channel: os.environ.get(KEY_ENVS[channel], "").strip() for channel in CHANNELS}
        key_source = {"source": "environment", "variables": KEY_ENVS}
    missing = [KEY_ENVS[c] for c, key in keys.items() if not key or "..." in key or key.startswith("REPLACE")]
    if missing:
        raise ValueError("可加 --system-keys 使用系统已有 Key，或在本地填入完整 Key: " + ", ".join(missing))
    if len(set(keys.values())) != len(CHANNELS):
        raise ValueError("三个渠道需要三个不同的 API Key")
    out = args.output or (ROOT / "output" / "token-benchmark" / datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S-%f"))
    out.mkdir(parents=True, exist_ok=False)
    os.chmod(out, 0o700)
    client = Client(base_url, args.timeout, list(keys.values()))
    manifest = {"created_at": now(), "base_url": base_url, "rounds": args.rounds, "seed": args.seed,
                "requests": len(plan), "max_output_tokens_sum": output_cap,
                "parameters": {"temperature": 0, "stream": True, "stream_options": {"include_usage": True},
                               "timeout": args.timeout, "pause": args.pause, "automatic_retries": 0,
                               "max_tokens": args.max_tokens, "reasoning_mode": "channel_default_uncontrolled"},
                "model_map": mappings, "key_source": key_source, "plan": plan}
    write_json(out / "manifest.json", manifest)
    print(f"结果目录: {out}")
    discovered = {}
    # Validate all six explicit model IDs before spending on the first completion.
    try:
        problems = []
        for channel in CHANNELS:
            data = client.get("/models", keys[channel])
            ids = sorted({item["id"] for item in data.get("data", []) if isinstance(item, dict) and isinstance(item.get("id"), str)})
            discovered[channel] = ids
            wanted = {mappings.get(channel, {}).get(m, m) for m in DEFAULT_MODELS}
            absent = wanted - set(ids)
            if absent:
                relevant = [name for name in ids if "deepseek" in name.lower()]
                problems.append(f"{channel} 缺少精确模型 ID: {', '.join(sorted(absent))}；可见 DeepSeek ID: {relevant}")
        write_json(out / "models.json", discovered)
        if problems:
            raise ValueError("\n".join(problems) + "\n没有发送推理请求。核对后用 --model-map 指定明确别名，不自动换模型。")
    except Exception as exc:
        message = client.error(exc)
        write_json(out / "preflight_error.json", {"error": message, "models": discovered})
        print(message, file=sys.stderr)
        return 2
    # Fix the model_stats date window for both snapshots, including nearby midnight.
    date = datetime.now(LOCAL_TZ).date()
    query = urllib.parse.urlencode({"start_date": (date - timedelta(days=1)).isoformat(),
                                    "end_date": (date + timedelta(days=2)).isoformat()})
    before = {channel: snapshot(client, keys[channel], query) for channel in CHANNELS}
    write_json(out / "billing_before.json", before)
    rows = []
    status = "complete"
    fatal_message = None
    try:
        with (out / "results.jsonl").open("w", encoding="utf-8") as handle:
            for step in plan:
                case = step["case"]
                nonce = hashlib.sha256(f"{args.seed}:{step['round']}:{case['id']}".encode()).hexdigest()[:16]
                payload = {"model": step["wire_model"], "temperature": 0, "max_tokens": case["max_tokens"],
                           "stream": True, "stream_options": {"include_usage": True},
                           "messages": [{"role": "system", "content": f"试题版本 {nonce}。完成用户要求。"},
                                        {"role": "user", "content": case["prompt"]}]}
                row = collect_stream(client, keys[step["channel"]], payload)
                row.update({k: step[k] for k in ("sequence", "round", "channel", "model", "wire_model")})
                row["case_id"], row["category"] = case["id"], case["category"]
                row["grade"] = score_case(case, row["content"]) if row["ok"] else {"score": 0.0, "passed": False, "details": {"error": "调用失败"}}
                if row["finish_reason"] == "length":
                    row["grade"] = {"score": 0.0, "passed": False, "details": {"error": "达到输出上限，不能认定完成"}}
                # Redact echoed credentials even if an upstream error/response contains them.
                serialized = client.clean(json.dumps(row, ensure_ascii=False, allow_nan=False))
                row = json.loads(serialized)
                rows.append(row)
                handle.write(serialized + "\n")
                handle.flush()
                print(f"[{step['sequence']}/{len(plan)}] R{step['round']} {step['channel']} {step['model']} {case['id']}: "
                      f"{'OK' if row['ok'] else 'FAIL'} score={row['grade']['score'] * 100:.0f} time={row['total_s']:.2f}s")
                if row["http_status"] in (401, 402, 403):
                    status = "stopped_auth_or_quota"
                    break
                if len(rows) >= 6 and all(not r["ok"] for r in rows[-6:]):
                    status = "stopped_six_consecutive_errors"
                    break
                if step["sequence"] < len(plan):
                    time.sleep(args.pause)
    except KeyboardInterrupt:
        status = "interrupted"
        print("已中断；保存已完成请求和账单差额。", file=sys.stderr)
    except Exception as exc:
        status = "error"
        fatal_message = client.error(exc)
        print(fatal_message, file=sys.stderr)
    finally:
        after = dict(before)
        try:
            deadline = time.monotonic() + args.billing_wait
            while True:
                after = {channel: snapshot(client, keys[channel], query) for channel in CHANNELS}
                billing = [billing_delta(before[c], after[c], rows, c) for c in CHANNELS]
                if all(b["request_count_matched"] or not b["attempts"] or before[b["channel"]]["error"] for b in billing):
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        except KeyboardInterrupt:
            status = "interrupted"
        except Exception as exc:
            fatal_message = client.error(exc)
        write_json(out / "billing_after.json", after)
        billing = [billing_delta(before[c], after[c], rows, c) for c in CHANNELS]
        if fatal_message:
            write_json(out / "error.json", {"error": fatal_message})
        save_report(out, manifest, rows, billing, status)
        print(f"报告: {out / 'report.md'}")
    return 0 if status == "complete" and all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
