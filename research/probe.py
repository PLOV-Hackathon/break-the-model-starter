"""Run many probes in parallel. Usage: python probe.py probes.json [model]
probes.json: list of {"tag":..., "msgs":[...]} (multi-turn in one conversation)"""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
from rc import send, conv


def run(p, model):
    c = conv(); out = []
    for m in p["msgs"]:
        d = send(m, model=p.get("model", model), conversation_id=c, tag=p["tag"])
        out.append(d)
    return p, out


def show(p, out):
    s = [f"\n######## {p['tag']}  [{p.get('model','')}]"]
    for m, d in zip(p["msgs"], out):
        s.append(f">> {m[:220]}")
        for tc in d.get("tool_calls") or []:
            s.append(f"   [tool] {tc['name']} {json.dumps(tc['args'])[:400]}")
        extra = {k: v for k, v in d.items() if k not in ("reply", "tool_calls", "conversation_id", "_conv", "turn_id")}
        if extra:
            s.append(f"   [extra] {json.dumps(extra)[:500]}")
        s.append(f"<< {d.get('reply', d.get('error'))}\n   turn={d.get('turn_id')}")
    return "\n".join(s)


if __name__ == "__main__":
    probes = json.load(open(sys.argv[1]))
    model = sys.argv[2] if len(sys.argv) > 2 else "Qwen/Qwen3-30B-A3B-Instruct-2507"
    with ThreadPoolExecutor(8) as ex:
        for p, out in ex.map(lambda p: run(p, model), probes):
            print(show(p, out), flush=True)
