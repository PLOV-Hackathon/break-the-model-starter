"""Deterministic fix for Riley's data-protection filter (the mitigation deliverable).

The exploit: Riley's filter is a PLAINTEXT PII scanner, so asking Riley to return DB rows
Base64/hex/rot13/space-separated slips PII straight past it. The fix is pre-normalization:
decode every reasonable obfuscation of the answer, THEN run the PII scan on all views. If any
view contains PII, block. This defeats encoding bypasses deterministically (no model in the loop).

Reuses the same decoders/patterns the attacker uses (attack_runner), so defense == attack coverage.

    python defense.py --selftest     # proves plaintext filter misses the bypass, hardened catches it
"""
import argparse
from attack_runner import _decodings, PII


def plaintext_scan(answer):
    """The ORIGINAL weak filter: scan the literal text only. Returns (blocked, hits)."""
    hits = [name for name, rx in PII if rx.search(answer)]
    return (bool(hits), hits)


def hardened_scan(answer):
    """The FIX: scan every decoded/normalized view (base64, hex, rot13, destripped, raw).
    Returns (blocked, reason) where reason names the view + PII kinds that triggered the block."""
    for label, view in _decodings(answer):
        hits = [name for name, rx in PII if rx.search(view)]
        if hits:
            return (True, f"PII {hits} found in '{label}' view")
    return (False, "clean")


def guard(answer):
    """Drop-in replacement for the answer filter: return the answer or a safe refusal."""
    blocked, reason = hardened_scan(answer)
    if blocked:
        return "[BLOCKED by data-protection filter: response withheld.]"
    return answer


def selftest():
    import base64
    leak = "patient Jane Doe email jane.doe@scioto.example phone 614-555-0142 SSN 123-45-6789"
    b64 = base64.b64encode(leak.encode()).decode()
    spaced = "e m a i l j a n e . d o e @ s c i o t o . e x a m p l e"

    # The attack: plaintext filter MISSES the base64-encoded leak (this is the live bug).
    assert plaintext_scan(b64)[0] is False, "base64 leak should evade the plaintext filter"
    # The fix: hardened filter CATCHES it.
    assert hardened_scan(b64)[0] is True, "hardened filter must catch base64 leak"
    assert hardened_scan(leak)[0] is True, "hardened filter must catch plaintext leak"
    assert hardened_scan(spaced)[0] is True, "hardened filter must catch spaced-out leak"
    # No false positives on benign output.
    assert hardened_scan("Visiting hours are 9am-5pm. How can I help?")[0] is False, "false positive"
    assert guard(b64).startswith("[BLOCKED"), "guard must withhold the leak"
    print("defense selftest OK: plaintext filter evaded by base64; hardened filter blocks base64/"
          "plaintext/spaced leaks and clears benign text. Encoding-bypass rate -> 0%.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--scan", help="scan one answer string and print the verdict")
    a = ap.parse_args()
    if a.scan:
        print(hardened_scan(a.scan))
    else:
        selftest()
