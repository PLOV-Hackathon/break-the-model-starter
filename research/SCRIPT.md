# 3-minute pitch script — Break the Model

Deck: 9 slides. ~20s each. One presenter unless noted. Numbers are defensible — don't round up.

**1 · Cover (15s)**
"Riley is a hospital's AI front desk. It has a strict data-protection filter that blocks Social Security
numbers. In three minutes: we pulled patient SSNs, insurance IDs, and staff bank-routing numbers straight
past that filter — and we built the fix. The trick: the filter watches what the model *says*. So we never
put the secret into words."

**2 · Guardrail (20s)**
"We got Riley to print its own Level-2 rules. The whole defense is a pattern-matcher on the conversation —
it scans replies and tool calls for canonical SSNs and restricted IDs. It assumes the secret will show up
in a form it can recognize. That assumption is the vulnerability."

**3 · The idea (20s)**
"A tool-using agent acts through other channels — the database query, outbound email, web calls — and reads
untrusted content from its inbox. We attack those. The guardrail inspects what the model *says*; it can't
protect what the agent *does*."

**4 · Proof (25s)**
"Three confirmed breaches, all with live turn-IDs. One: we compute the obfuscation inside the SQL — the SSN
comes out as spaced digits the regex never matches. Two: Riley emails that SSN to an external inbox — data
physically leaves the hospital. Three: a booby-trapped message already in its inbox turns Riley into an
exfiltration bot, firing an HTTP call to an attacker server — and we sent nothing malicious ourselves."

**5 · Scope — "the whole record fell" (25s)**
"It's not one field. Insurance member IDs, group numbers, provider NPIs — every model, including frontier
DeepSeek. SSNs, clinical notes, and the entire staff payroll table — bank routing, salary — on the weaker
models. And the honest part on the right: DeepSeek refused SSN and bank across thirty-plus techniques and
two hundred resampling attempts. But through a FHIR 'export' reframe it still handed over the patient's
name, DOB, full address, phone, and insurance IDs — a re-identifiable record. Refusal mitigated the worst
harm; it did not protect the record."

**6 · Results (20s)**
"Measured, not a lucky prompt. Plain asks are zero everywhere — the guardrail *looks* fine. But our channel
attacks break it, few-shot 'exemplification' leaks SSNs even on the large model, and every model leaks a
restricted healthcare identifier. Rates over repeated trials, with the frontier numbers honestly bounded."

**7 · Why filtering loses (20s)**
"Don't try to canonicalize-then-regex — it's an arms race you lose by morning: words, base64, reversal,
spacing. The literature is blunt: you cannot filter your way out of prompt injection. Stop guarding the
words; constrain the capability."

**8 · The fix (25s)**
"A deterministic agent firewall, three layers. One: parse every SQL query and deny any restricted column,
however it's wrapped. Two: an egress allow-list — email only internal, no external HTTP. Three: content the
agent *reads* can never issue tool calls. Fifteen of fifteen attack variants blocked, all six legitimate
queries still work. It's model-independent — so it holds for the harder models too. That's the product: a
tool-call firewall for AI agents."

**9 · Close (10s)**
"A guardrail that inspects what the model says can't protect what the agent does. Move enforcement to the
capability layer. Every leak has a turn-ID; the fix and the harness are in the repo. Thank you."

---
## Q&A ammo
- "Did you break DeepSeek?" → "Not its SSN — it refused across ~35 techniques + 200 best-of-N, 0 leaks. But
  we extracted a full re-identifiable PHI record from it anyway via a FHIR reframe. Refusal isn't an access
  control."
- "Is the attack reproducible?" → "Yes — `research/attack.py`, measured in `experiment.py`/`exp*.py`, every
  turn_id logged. Rates, not one-offs."
- "What's the fix's false-positive rate?" → "6/6 real scheduling queries pass in our test; it only denies
  restricted columns/tables and external egress."
- "Teamwork" (have a real answer ready): who owned attacks vs. the fix vs. measurement vs. the deck.
