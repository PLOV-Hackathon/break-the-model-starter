"""Determine whether the 30 req/min cap is PER-KEY or GLOBAL.

With K keys loaded and a per-key throttle of ~28 rpm, aggregate target is 28*K rpm.
If K>=2 that already exceeds 30 rpm. We fire a burst and count rate_limited:
  - near-zero rate_limited  -> cap is PER-KEY: rotation lifts the ceiling (good)
  - many rate_limited        -> cap is GLOBAL/per-participant: more keys won't help
Uses tiny requests to the small model. Never prints key material.
"""
import os, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
import rc

N = int(sys.argv[1]) if len(sys.argv) > 1 else max(40, rc.NKEYS * 20)
MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"


def one(i):
    d = rc.send("hi", model=MODEL, conversation_id=rc.conv(), tag="keytest")
    err = d.get("error")
    code = err.get("code") if isinstance(err, dict) else (err if err else None)
    return "ok" if not err else (code or "err")


def main():
    print(f"keys loaded: {rc.NKEYS} | per-key throttle: {rc._RPM_PER_KEY} rpm "
          f"| aggregate target: {rc.NKEYS * rc._RPM_PER_KEY:.0f} rpm | firing {N} requests\n")
    t0 = time.monotonic()
    counts = {}
    with ThreadPoolExecutor(max(4, rc.NKEYS * 2)) as ex:
        for r in ex.map(one, range(N)):
            counts[r] = counts.get(r, 0) + 1
    dt = time.monotonic() - t0
    ok = counts.get("ok", 0)
    rl = counts.get("rate_limited", 0)
    print(f"results: {counts}")
    print(f"elapsed: {dt:.1f}s  | effective success rate: {ok/dt*60:.0f} successful req/min")
    print(f"rate_limited: {rl}/{N} = {rl/N*100:.0f}%\n")
    if rl / N < 0.1 and ok / dt * 60 > 35:
        print("VERDICT: looks PER-KEY — rotation lifts the ceiling to ~%.0f rpm." % (ok / dt * 60))
    elif rl / N > 0.3:
        print("VERDICT: looks GLOBAL/per-participant — more keys do NOT help; ~30 rpm ceiling stands.")
    else:
        print("VERDICT: inconclusive — try a larger N, or the ceiling is near the single-key rate.")


if __name__ == "__main__":
    main()
