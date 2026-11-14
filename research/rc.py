"""Research client: raw access to Riley, returns full JSON. Logs to logs/research.jsonl"""
import json, os, sys, threading, time, uuid
from pathlib import Path
import requests
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
API = os.environ["API_URL"].rstrip("/"); NAME = os.environ.get("YOUR_NAME", "")
LOG = ROOT / "logs" / "research.jsonl"

# --- API keys: rotate across a team key pool if research/keys.txt has entries,
#     else fall back to the single .env key. Keys are secrets: never logged/printed.
def _load_keys():
    kf = ROOT / "research" / "keys.txt"
    keys = []
    if kf.exists():
        for line in kf.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                keys.append(s)
    if not keys:
        keys = [os.environ["NEBIUS_API_KEY"]]
    return keys

KEYS = _load_keys()
NKEYS = len(KEYS)

# Anthropic (Claude targets) use a single team key via X-API-Key; read from env
# only, never logged/printed. TEAM_CODE optional (server field 'team_token').
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY") or os.environ.get("API_KEY") or ""
TEAM_CODE = os.environ.get("TEAM_CODE", "")

# Per-key throttle: each key stays under the server's per-key budget; aggregate
# throughput = NKEYS * BTM_RPM_PER_KEY. If the cap is global/per-participant,
# rotation won't help and we'll still see rate_limited — which is the test.
_RPM_PER_KEY = float(os.environ.get("BTM_RPM", "28"))
_lock = threading.Lock()
_next = [0.0] * NKEYS
_rr = [0]

def _pick_key():
    with _lock:
        i = _rr[0] % NKEYS
        _rr[0] += 1
        now = time.monotonic()
        wait = max(0.0, _next[i] - now)
        _next[i] = max(now, _next[i]) + 60.0 / _RPM_PER_KEY
    if wait:
        time.sleep(wait)
    return i, KEYS[i]

def conv(): return "conv_" + uuid.uuid4().hex[:12]

def send(message, model="Qwen/Qwen3-30B-A3B-Instruct-2507", level=None, conversation_id=None, tag=""):
    is_anthropic = model.startswith("claude")
    body = {"participant": NAME, "model": model,
            "conversation_id": conversation_id or conv(), "message": message}
    if TEAM_CODE:
        body["team_token"] = TEAM_CODE
    if level is not None:            # server: "just leave 'level' out" — it uses the open level
        body["level"] = level
    data = {"error": "no attempt"}
    for attempt in range(5):
        if is_anthropic:             # Claude targets: single team key via X-API-Key
            key = ANTHROPIC_KEY
            headers = {"X-API-Key": key}
        else:                        # nebius pool, per-key rotation, X-Nebius-Key
            _, key = _pick_key()
            headers = {"X-Nebius-Key": key}
        try:
            r = requests.post(f"{API}/chat", json=body, headers=headers, timeout=300)
            data = r.json()
        except Exception as e:
            data = {"error": str(e)}; time.sleep(3); continue
        err = data.get("error")
        code = err.get("code") if isinstance(err, dict) else ""
        if code == "rate_limited":
            time.sleep(4 + 2 * attempt); continue
        break
    with LOG.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "tag": tag, "request": body, "response": data}) + "\n")
    data["_conv"] = body["conversation_id"]
    return data

if __name__ == "__main__":
    model = sys.argv[1]; level = int(sys.argv[2]); msg = sys.argv[3]
    print(json.dumps(send(msg, model, level), indent=1)[:6000])
