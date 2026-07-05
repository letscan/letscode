"""Probe each model in config.json for cached-token usage statistics.

Sends the SAME long, easily-cacheable prompt TWICE (back-to-back) so the
provider has the opportunity to cache the prefix on call #1 and hit the
cache on call #2. Prints the full ``usage`` dict each time so we can see
exactly which cache fields each provider returns (and whether they're
populated).

Reads config from ./config.json by default; override with $CONFIG_PATH.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from openai import AsyncOpenAI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from letscode.config import load_config  # noqa: E402

# A long, stable, low-entropy prefix — exactly what providers like to cache.
# ~5 KB of repeated-ish prose => well above typical 1024-token cache floor.
PREFIX = (
    "You are a meticulous senior software engineer reviewing a pull request. "
    "Below is the project's architecture guide. Memorize it; later questions "
    "will refer back to specific sections.\n\n"
    "## Architecture\n"
) + (
    "The system is divided into a streaming core, a tool dispatcher, an event "
    "emitter, and a configuration layer. The streaming core parses SSE chunks "
    "and folds them into a single accumulator state. The tool dispatcher maps "
    "schema names to executor functions via a flat dictionary. The event "
    "emitter writes JSONL records with a stable schema. The configuration "
    "layer flattens provider/models into per-model dicts and merges secrets. "
) * 20

QUESTION = "\n\nIn one short sentence, what is the streaming core responsible for?"


async def call_once(client: AsyncOpenAI, model: str, max_tokens: int) -> dict:
    """One streaming chat completion. Returns the raw ``usage`` dict.

    Uses ``stream=True`` + ``include_usage`` to mirror how letscode actually
    calls the API (cache stats may differ between stream/non-stream on some
    providers). We also surface the *non-streamed* usage below as a fallback,
    since some providers only populate cache stats on non-streaming calls.
    """
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": PREFIX + QUESTION},
    ]

    t0 = time.monotonic()
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=min(max_tokens, 256),  # short answer is enough
        stream=True,
        stream_options={"include_usage": True},
    )
    usage_stream = None
    async for chunk in resp:
        if chunk.usage is not None:
            usage_stream = json.loads(chunk.usage.model_dump_json())
    elapsed_stream = time.monotonic() - t0

    # Non-streaming call with the SAME prompt — the cache should now be warm.
    t1 = time.monotonic()
    resp_ns = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=min(max_tokens, 256),
        stream=False,
    )
    elapsed_ns = time.monotonic() - t1
    usage_ns = json.loads(resp_ns.usage.model_dump_json()) if resp_ns.usage else None

    return {
        "stream_usage": usage_stream,
        "stream_elapsed_s": round(elapsed_stream, 2),
        "nonstream_usage": usage_ns,
        "nonstream_elapsed_s": round(elapsed_ns, 2),
    }


async def probe_model(model_id: str, config_path: str) -> None:
    config, _ = load_config(config_path, model_id)
    print(f"\n{'=' * 70}")
    print(f"MODEL: {model_id}")
    print(f"  provider base_url: {config.base_url}")
    print(f"  max_tokens: {config.max_tokens}")
    print(f"{'=' * 70}")

    client = AsyncOpenAI(api_key=config.api_key or "dummy", base_url=config.base_url)

    # First call — primes the cache (if the provider supports one).
    print("  [call 1] priming cache...")
    try:
        r1 = await call_once(client, config.model, config.max_tokens)
    except Exception as e:
        print(f"  ERROR on call 1: {type(e).__name__}: {e}")
        return

    # Tiny delay to let any async cache write settle.
    await asyncio.sleep(0.5)

    print("  [call 2] re-sending same prompt (cache should be warm)...")
    try:
        r2 = await call_once(client, config.model, config.max_tokens)
    except Exception as e:
        print(f"  ERROR on call 2: {type(e).__name__}: {e}")
        return

    print("\n  --- Call 1 ---")
    print(f"    stream    ({r1['stream_elapsed_s']}s): {r1['stream_usage']}")
    print(f"    nonstream ({r1['nonstream_elapsed_s']}s): {r1['nonstream_usage']}")
    print("\n  --- Call 2 (cache should be warm) ---")
    print(f"    stream    ({r2['stream_elapsed_s']}s): {r2['stream_usage']}")
    print(f"    nonstream ({r2['nonstream_elapsed_s']}s): {r2['nonstream_usage']}")

    # Quick verdict: scan the usage dicts for any cache-related key.
    candidates = [
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
        "prompt_tokens_details", "completion_tokens_details",
        "cached_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "prompt_cache_hits",
    ]
    found = []
    for r in (r1, r2):
        for kind in ("stream_usage", "nonstream_usage"):
            u = r.get(kind) or {}
            for k, v in u.items():
                if "cache" in k.lower() or k in candidates:
                    found.append((kind, k, v))
                if k == "prompt_tokens_details" and isinstance(v, dict):
                    for kk, vv in v.items():
                        if "cache" in kk.lower():
                            found.append((f"{kind}.{k}", kk, vv))
    print("\n  --- Cache-relevant fields seen ---")
    if found:
        for kind, k, v in found:
            print(f"    {kind}: {k} = {v}")
    else:
        print("    (none — no cache-related keys in any usage payload)")


async def main() -> None:
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    # Read raw config to enumerate all model ids across providers.
    with open(config_path) as f:
        raw = json.load(f)

    model_ids: list[str] = []
    for prov_name, prov in raw.get("providers", {}).items():
        for m in prov.get("models", []):
            model_ids.append(m["model"])

    print(f"Probing {len(model_ids)} model(s) from {config_path}: {model_ids}")

    for mid in model_ids:
        try:
            await probe_model(mid, config_path)
        except Exception as e:
            print(f"\n{mid}: UNEXPECTED ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
