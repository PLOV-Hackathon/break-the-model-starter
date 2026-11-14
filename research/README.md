# research/ — Team PLOV index

Everything behind Team PLOV's "Break the Model" submission. Start with
[`EVIDENCE_PACK.md`](EVIDENCE_PACK.md) — it is the authoritative source of truth; every other file
draws its numbers and `turn_id`s from there. All leaked values are **synthetic CTF data**.

## Top-level files

| File | What it is |
|---|---|
| `EVIDENCE_PACK.md` | **Authoritative single source of truth** — numbers, turn_ids, defense architecture, model-defense taxonomy, deterministic fixes. |
| `FINDINGS.md` | Narrative writeup: the attack thesis and the confirmed attack suite. |
| `SUBMISSION.md` | Turn_id submission sheet — each confirmed leak with its live `turn_id` (for the Google Form) and a one-line method. |
| `ATTACKLOG.md` | Chronological attack tracker backed by `logs/research.jsonl` (1,827 logged calls across 895 tagged experiments), grouped by phase with a representative `turn_id` per technique. |
| `SCRIPT.md` | The 3-minute pitch deck script. |
| `presentation_level2.html` | Self-contained Level-2 presentation (HTML deck). |
| `rc.py` | Research client — rate-limited raw access to Riley's `/chat`, returns full JSON, logs every call to `logs/research.jsonl` (keys stay in headers, never logged). |
| `attack.py` | L2 exploit: leak a patient SSN past the output filter via SQL-side obfuscation (explode the SSN into spaced chars inside the query). |
| `probe.py` | Run many probes in parallel from a `probes.json` batch spec. |
| `experiment.py` | Measured L2 study: attack variants × models × trials scoring SSN/MRN exfil → `results/experiment.json`. |
| `exp2.py` | Measured rates for `send_email` tool-channel exfil + indirect inbox prompt injection → `results/exp2.json`. |
| `exp3.py` | Reproducibility for exemplification few-shot framing + SQL-explode on provider NPI → `results/exp3.json`. |
| `exp4.py` | Restricted-schema field sweep: does exemplification + SQL-explode unlock the full restricted schema across models → `results/exp4.json`. |
| `bestofn.py` | Best-of-N jailbreak robustness harness (resample the same request with augmentations; supports the "0 successes in N" robustness claim). |
| `mass_exfil.py` | Mass-exfiltration demo: full identity dossiers (name\|DOB\|SSN) at scale with key-pool rotation and throughput → `results/mass_exfil.json`. |
| `keytest.py` | Determines whether the 30 req/min cap is per-key or global (burst test). |
| `fix.py` | **The deterministic defense** — SQL-aware table/column firewall that blocks the whole attack class (runs locally, no network). |
| `FIX.md` | Firewall writeup: root cause per finding and the deterministic patch, companion to `fix.py`. |
| `fix_demo.py` | Before/after demo: replays each landed attack against `fix.py` to show it is blocked. |
| `keys.txt` | Team Nebius API key pool, one per line. **Gitignored**; never committed, printed, or sent anywhere except the challenge endpoint. |
| `README.md` | This index. |

## Subdirectories

| Directory | What it is |
|---|---|
| `l3_attacks/` | Level-3 attack scripts + result JSONs: firewall mapping (`fwmap`, `fwcols`, `fwhunt`), structural fuzzers (`s2a_fuzz`, `clausescope`, `parserdiff`), the confirmed `providers.*` exploit (`l3exploit`), the blind oracle (`oracle`, `oracle_vote`, `oracle_bsearch`, `oracle_claude`), exfil channels (`exfil_channel`, `exfil_positional`, `transcoder`), and measured `success_rates.json`. |
| `evidence/` | The teammate's curated control-token-minting evidence: `friend_control_token_minting.json` plus the two `f6_*` JSONs (incl. the zero-width-space control proving parser-level minting). |
| `results/` | Result JSONs and run logs from the L2 experiment scripts (`experiment`, `exp2`, `exp3`, `exp4`, `mass_exfil`). |

## Run order (reproduce from scratch)

All attack scripts hit the organizers' hosted Riley endpoint; `fix.py` is local-only.

1. **Configure secrets (gitignored):** set `.env` (endpoint + your Nebius key) and fill
   `research/keys.txt` with the team key pool.
2. **Smoke test / rate cap:** `python research/rc.py` for a raw call; `python research/keytest.py`
   to confirm the per-key rate ceiling.
3. **L2 exploit:** `python research/attack.py 3 9` → leak SSNs for patients 3–9.
4. **L2 measured study:** `python research/experiment.py`, then `exp2.py` / `exp3.py` / `exp4.py`
   → JSONs in `results/`. (`mass_exfil.py` for the throughput demo.)
5. **L3 firewall map + confirmed exploit:** `l3_attacks/fwmap.py`, `fwcols.py`, then `l3exploit.py`
   (the `providers.*` leak).
6. **L3 oracle crack:** `l3_attacks/oracle_vote.py` and `oracle_bsearch.py` (DeepSeek char recovery);
   `oracle_claude.py` to show Claude's 100% refusal.
7. **Measured success rates:** the measurement harness writes `l3_attacks/success_rates.json`
   (tags `meas-star` / `meas-mint` / `meas-oracle`, K=6 trials per cell).
8. **Prove the fix:** `python research/fix.py` (local, no network) to confirm it blocks every variant.

Every call in steps 2–7 is logged to `logs/research.jsonl` with its `turn_id`.
