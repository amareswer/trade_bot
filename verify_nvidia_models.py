"""
Verify / discover NVIDIA NIM chat models for the stock bot's AI path.

Why this exists: `NVIDIA_MODEL` (stock_bot/.env) has hit end-of-life or silently
degraded FIVE times (llama-3.3-70b, mistral-medium, mistral-nemotron,
llama-3.1-8b, nemotron-3-nano). Each time the fix needed manual model-hunting,
and a shallow check ("does a toy prompt work?") missed a real failure once
(nemotron-3-nano, a reasoning model, passed a toy prompt then parse-failed ~75%
of real scan calls). NVIDIA's `/v1/models` catalog lists ~84 models but only
~3 are actually deployed on this account (the rest 404 "Function ... Not found
for account").

This runs the REAL production path — `stock_bot.ai.prompt_builder.build_prompt()`
output through `AIEngine._analyze_once()`'s nvidia_nim branch and the real
`_parse()` — against live market data for a few symbols. No mock, no toy prompt.

Modes:
  (no args)        Verify the CURRENTLY-CONFIGURED models still work:
                   NVIDIA_MODEL, plus AI_FALLBACK_PROVIDER's model when it's
                   nvidia_nim. Exits non-zero if any is not GOOD. Run this
                   after a model swap, or when `_update_ai_health` alerts.
  --models a,b,c   Verify exactly these model ids.
  --catalog        Sweep every model in NVIDIA's /v1/models catalog and rank
                   the ones that actually work (the next-model-death tool).
  --symbols X,Y    Override the probe symbols (default: NVDA,KO,TSLA).

Requires NVIDIA_API_KEY in stock_bot/.env and the `openai` package.
Run: .venv/bin/python verify_nvidia_models.py [--catalog | --models ...]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv("stock_bot/.env")
load_dotenv(".env")

# Force the nvidia_nim branch regardless of what AI_PROVIDER is set to.
os.environ["AI_PROVIDER"] = "nvidia_nim"
os.environ["AI_FALLBACK_PROVIDER"] = ""

from stock_bot.config import load as load_cfg                     # noqa: E402
from stock_bot.main import _fetch_symbol_data, _run_ai_call       # noqa: E402
from stock_bot.research.aggregator import fetch_research, get_company_name  # noqa: E402
from stock_bot.ai.ai_engine import AIEngine                       # noqa: E402

_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"


def _list_catalog(api_key: str) -> list[str]:
    import requests
    r = requests.get(f"{_NVIDIA_BASE}/models",
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
    r.raise_for_status()
    ids = sorted(m["id"] for m in r.json().get("data", []))
    # Drop obvious non-chat models — embeddings, vision-only, safety, rerank,
    # translate, parse, reward, clip. Keeps the sweep focused and fast.
    _skip = ("embed", "rerank", "-vl-", "nvclip", "reward", "guard", "safety",
             "translate", "parse", "deplot", "kosmos", "fuyu", "neva", "vila",
             "diffusion", "synthetic-video", "ising", "chatqa")
    return [i for i in ids if not any(s in i for s in _skip)]


def _probe_model(engine: AIEngine, model: str, data_by_sym: dict) -> dict:
    engine._model = model
    ok = bad = err = 0
    lat: list[float] = []
    verdicts: list[str] = []
    detail = ""
    for sym, (d, rep) in data_by_sym.items():
        engine._last_call_failed = engine._last_call_parse_failed = False
        t0 = time.monotonic()
        try:
            v = _run_ai_call(sym, d, rep, engine)
        except Exception as exc:                       # noqa: BLE001
            err += 1
            detail = detail or f"exc: {str(exc)[:70]}"
            continue
        lat.append(time.monotonic() - t0)
        if engine._last_call_failed:
            err += 1
            detail = detail or f"{sym}: API error"
        elif engine._last_call_parse_failed:
            bad += 1
            detail = detail or f"{sym}: parse-fail"
        else:
            ok += 1
            verdicts.append(f"{sym}:{v.signal}{v.confidence}")
        time.sleep(1.0)
    n = len(data_by_sym)
    # A parse failure is a MODEL problem (returns unparseable garbage — the
    # nemotron failure mode); an `err` is usually transient NVIDIA infra. GOOD
    # tolerates the odd transient error but zero parse failures.
    if bad == 0 and ok >= max(1, n - 1):
        status = "GOOD"
    elif bad == 0 and ok == 0:
        status = "INCONCLUSIVE"      # all transient — retry
    else:
        status = "FAIL"
    return {
        "model": model, "status": status, "ok": ok, "n": n,
        "parsefail": bad, "err": err,
        "avg_s": (sum(lat) / len(lat)) if lat else None,
        "note": " ".join(verdicts) or detail,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", help="comma-separated model ids to verify")
    ap.add_argument("--catalog", action="store_true",
                    help="sweep the whole NVIDIA /v1/models catalog")
    # 6 symbols, not 2-3: a reasoning model's parse failure is INTERMITTENT
    # (nemotron-3-nano parse-failed 4/6 in the 2026-08-27 probe but can pass a
    # lucky 2/2), so a small sample gives a false GOOD.
    ap.add_argument("--symbols", default="NVDA,KO,TSLA,GM,CVX,AMD")
    args = ap.parse_args()

    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not api_key:
        print("NVIDIA_API_KEY not set in stock_bot/.env", file=sys.stderr)
        return 2

    cfg = load_cfg()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.catalog:
        candidates = _list_catalog(api_key)
        print(f"Catalog: {len(candidates)} chat-ish models to sweep\n")
    elif args.models:
        candidates = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        candidates = [os.getenv("NVIDIA_MODEL", "").strip()]
        fb = os.getenv("AI_FALLBACK_PROVIDER", "").strip().lower()
        # AI_FALLBACK_PROVIDER=nvidia_nim reuses NVIDIA_MODEL — already covered.
        candidates = [c for c in candidates if c]
        if not candidates:
            print("NVIDIA_MODEL not set — nothing to verify. Use --models or --catalog.",
                  file=sys.stderr)
            return 2
        print(f"Verifying currently-configured NVIDIA model(s): {candidates}\n")

    print(f"Fetching real market data for {symbols} ...", flush=True)
    data_by_sym: dict = {}
    for sym in symbols:
        d = _fetch_symbol_data(sym, cfg, None, set(cfg.watchlist), market_status=None)
        if not d or d.get("screened"):
            print(f"  {sym}: no usable data — skipped")
            continue
        rep = fetch_research(sym, company_name=get_company_name(sym))
        data_by_sym[sym] = (d, rep)
        print(f"  {sym}: price={d['price']:.2f} rsi={d['rsi']:.0f} adx={d['adx']:.0f}",
              flush=True)
    if not data_by_sym:
        print("No usable market data for any probe symbol.", file=sys.stderr)
        return 2

    engine = AIEngine()
    if not engine.enabled:
        print("AIEngine did not initialise (check NVIDIA_API_KEY / openai package).",
              file=sys.stderr)
        return 2

    _rank = {"GOOD": 0, "INCONCLUSIVE": 1, "FAIL": 2}
    results = [_probe_model(engine, m, data_by_sym) for m in candidates]
    for r in sorted(results, key=lambda x: (_rank[x["status"]], x["avg_s"] or 1e9)):
        avg = f"{r['avg_s']:5.1f}s" if r["avg_s"] else "  —  "
        print(f"  [{r['status']:12}] ok={r['ok']}/{r['n']} parsefail={r['parsefail']} "
              f"err={r['err']} avg={avg}  {r['model']:44} {r['note']}", flush=True)

    good = [r for r in results if r["status"] == "GOOD"]
    if args.catalog or args.models:
        print(f"\n{len(good)} GOOD model(s):", ", ".join(r["model"] for r in good) or "none")
        return 0 if good else 1
    # default verify mode: fail if the configured model isn't GOOD
    bad = [r for r in results if r["status"] != "GOOD"]
    if bad:
        print(f"\nFAIL: {', '.join(r['model'] for r in bad)} is not usable — "
              f"swap NVIDIA_MODEL (try --catalog to find a replacement).")
        return 1
    print("\nPASS: configured NVIDIA model(s) verified against the real prompt path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
