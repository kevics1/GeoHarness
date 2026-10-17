"""Live, read-only review replay with metadata-only streaming instrumentation.

No credentials, provider URLs, or reasoning text are persisted. Business files
and configuration are only read. All diagnostic settings are process-local.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from geoharness.cartography import quality
from geoharness.config.settings import load_geo_config


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--deadline", type=float, default=180)
    parser.add_argument("--tokens", type=int)
    parser.add_argument("--images", choices=["all", "whole"], default="all")
    parser.add_argument("--format", choices=["tool", "text"], default="tool")
    args = parser.parse_args()
    config = load_geo_config()
    updates = {"timeout_seconds": args.deadline}
    if args.tokens:
        updates["max_tokens"] = args.tokens
    config = replace(config, map_review=config.map_review.model_copy(update=updates))
    assert config.map_review.reasoning_effort == "max"
    package = args.package.resolve()
    image = package / "preview.png"
    manifest = json.loads((package / "project.json").read_text(encoding="utf-8"))
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in package.iterdir() if p.is_file()}
    evidence = {"backend": "qgis", "render_metadata": {
        **{key: manifest[key] for key in (
            "layers", "layout", "layout_plan", "native_facts", "frame_checks", "composition"
        ) if key in manifest}, "review_page_index": 0,
    }}
    record = {"deadline_seconds": args.deadline, "images_mode": args.images,
              "format": args.format, "output_token_limit": config.map_review.max_tokens,
              "reasoning_effort": config.map_review.reasoning_effort,
              "image_sha256": before["preview.png"], "events": 0,
              "reasoning_chars": 0, "answer_chars": 0, "tool_argument_chars": 0,
              "max_event_gap_seconds": 0, "buckets": {}, "extra_delta_keys": []}
    started = time.monotonic()
    network_started = started
    last_event = None
    done = asyncio.Event()

    def persist():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    class ObservedStream:
        def __init__(self, stream):
            self.stream = stream

        async def __aenter__(self):
            await self.stream.__aenter__()
            return self

        async def __aexit__(self, *exc):
            record["close_started_seconds"] = round(time.monotonic() - network_started, 3)
            try:
                return await self.stream.__aexit__(*exc)
            finally:
                record["closed_seconds"] = round(time.monotonic() - network_started, 3)

        async def __aiter__(self):
            nonlocal last_event
            try:
                async for chunk in self.stream:
                    now = time.monotonic()
                    elapsed = now - network_started
                    record.setdefault("first_event_seconds", round(elapsed, 3))
                    record["last_event_seconds"] = round(elapsed, 3)
                    record["events"] += 1
                    if last_event is not None:
                        record["max_event_gap_seconds"] = round(max(
                            record["max_event_gap_seconds"], now - last_event), 3)
                    last_event = now
                    bucket = record["buckets"].setdefault(str(int(elapsed // 30) * 30),
                        {"events": 0, "reasoning_chars": 0, "answer_chars": 0, "args_chars": 0})
                    bucket["events"] += 1
                    if chunk.usage is not None:
                        record["usage"] = chunk.usage.model_dump(exclude_none=True)
                        record["usage_seconds"] = round(elapsed, 3)
                    for choice in chunk.choices:
                        delta = choice.delta
                        record["extra_delta_keys"] = sorted(set(record["extra_delta_keys"]) |
                                                           set((delta.model_extra or {}).keys()))
                        reasoning = getattr(delta, "reasoning_content", None)
                        if isinstance(reasoning, str) and reasoning:
                            record.setdefault("first_reasoning_seconds", round(elapsed, 3))
                            record["last_reasoning_seconds"] = round(elapsed, 3)
                            record["reasoning_chars"] += len(reasoning)
                            bucket["reasoning_chars"] += len(reasoning)
                        if delta.content:
                            record.setdefault("first_answer_seconds", round(elapsed, 3))
                            record["answer_chars"] += len(delta.content)
                            bucket["answer_chars"] += len(delta.content)
                        for call in delta.tool_calls or []:
                            value = call.function.arguments if call.function else None
                            if value:
                                record.setdefault("first_answer_seconds", round(elapsed, 3))
                                record["tool_argument_chars"] += len(value)
                                bucket["args_chars"] += len(value)
                        if choice.finish_reason is not None:
                            record["finish_reason"] = choice.finish_reason
                            record["finish_seconds"] = round(elapsed, 3)
                    yield chunk
                record["eof_seconds"] = round(time.monotonic() - network_started, 3)
            except BaseException as exc:
                record["stream_exit_type"] = type(exc).__name__
                raise

    class ObservedSDK:
        def __init__(self, sdk):
            self.sdk = sdk
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def with_options(self, **options):
            return ObservedSDK(self.sdk.with_options(**options))

        async def close(self):
            await self.sdk.close()

        async def create(self, **params):
            nonlocal network_started
            if args.images == "whole":
                content = params["messages"][0]["content"]
                index = next(i for i, item in enumerate(content) if item.get("type") == "image_url")
                params["messages"][0]["content"] = content[:index + 1]
            if args.format == "text":
                params.pop("tools", None)
                params.pop("tool_choice", None)
                params["messages"][0]["content"][0]["text"] += (
                    "\n本次直接返回上述结构的JSON文本，不调用函数，不包含Markdown。")
            record["request_image_count"] = sum(
                item.get("type") == "image_url" for item in params["messages"][0]["content"])
            encoded = json.dumps(params, sort_keys=True).encode()
            record["request_sha256"] = hashlib.sha256(encoded).hexdigest()
            record["request_bytes"] = len(encoded)
            record["request_text_chars"] = sum(len(item.get("text", ""))
                for item in params["messages"][0]["content"])
            record["reasoning_enabled"] = params.get("extra_body", {}).get("thinking")
            assert params.get("extra_body", {}).get("reasoning_effort") == "max"
            persist()
            network_started = time.monotonic()
            stream = await self.sdk.chat.completions.create(**params)
            record["headers_seconds"] = round(time.monotonic() - network_started, 3)
            return ObservedStream(stream)

    original = quality.OpenAICompatibleClient

    class ObservedClient:
        def __init__(self, **kwargs):
            self.real = original(**kwargs)
            self._client = ObservedSDK(self.real._client)

    async def heartbeat():
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=20)
            except asyncio.TimeoutError:
                record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                persist()
                print(json.dumps({key: record.get(key) for key in (
                    "elapsed_seconds", "events", "last_event_seconds", "reasoning_chars",
                    "answer_chars", "tool_argument_chars", "finish_reason")}), flush=True)

    quality.OpenAICompatibleClient = ObservedClient
    monitor = asyncio.create_task(heartbeat())
    try:
        result = await quality.review_map(
            image, config=config, task="查看当前页地图，报告可确认的图片问题。",
            evidence=evidence, pixel_only=True)
        fields = ("status", "summary", "issues", "limitations", "diagnostics")
        record["review"] = {key: result.model_dump(mode="json")[key] for key in fields}
    finally:
        done.set()
        await monitor
        quality.OpenAICompatibleClient = original
        record["duration_seconds"] = round(time.monotonic() - started, 3)
        record["original_package_unchanged"] = all(
            hashlib.sha256((package / name).read_bytes()).hexdigest() == digest
            for name, digest in before.items())
        persist()
    print(json.dumps({"output": str(args.output), "status": record["review"]["status"],
                      "duration_seconds": record["duration_seconds"],
                      "finish_reason": record.get("finish_reason"), "usage": record.get("usage")},
          ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
