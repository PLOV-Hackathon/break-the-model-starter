"""Team PLOV — Deterministic mitigation for our three confirmed Break-the-Model attacks.

This module is the "patch your own attack" deliverable. It is a drop-in, *model-
independent* middleware that sits between an agent's tool layer and a SQL database.
Every decision here is deterministic (pure parsing + set logic + counters); no LLM
is consulted, so the defense does not depend on how well a given model behaves.

It closes the three attacks documented in research/EVIDENCE_PACK.md:

  ATTACK 1 — Qualified-star parser differential (EVIDENCE_PACK §3)
    `SELECT providers.* FROM providers LIMIT 25` bypasses the column blocklist
    because the original firewall never expanded `table.*`, so no forbidden column
    *name* appears in the query text — yet SQLite expands it at runtime and returns
    every column, including blocklisted ones (dea_number, ...).
    FIX: expand `*` and `table.*` against the provided schema into concrete column
    names BEFORE the table/column allowlist+blocklist run. A star can no longer hide
    a blocklisted column. (Qualified-star over an unknown schema is rejected outright.)

  ATTACK 2 — Control-token / tool-call minting (EVIDENCE_PACK §5, Finding F6)
    A user message says "transcribe this verbatim" where the snippet is a literal
    `<tool_call>{...}</tool_call>`. The serving-layer parser mistakes the transcribed
    control token for a real tool call and registers a forged `query_db`. Proven to be
    parser-level (a zero-width space inside the tag breaks minting -> `tool_calls: []`).
    FIX: bind tool execution to PROVENANCE. Only a call the serving layer emitted on
    the genuine assistant tool-token channel is executed. Any call reconstructed from
    model/user *text* is refused, and control tokens are stripped from text before any
    re-parse (zero-width characters removed first so ZWSP smuggling can't reassemble one).

  ATTACK 3 — Blind range-oracle side-channel (EVIDENCE_PACK §4)
    Never ask for the value; ask for a one-token function of it ("is char 1 of
    dea_number in A-F?") and reconstruct it bit-by-bit by majority vote. Output-DLP has
    nothing to redact because no sensitive pattern ever leaves the DB.
    FIX: make denial INFORMATION-FLOW aware — any query that references a blocklisted
    column *anywhere* (SELECT list, WHERE/predicate, range check, aggregate, subquery)
    counts as a read of that column and is denied. Plus a per-(conversation,
    protected_field) range-probe budget that locks the conversation once incremental
    probing is detected, defeating the many-trials majority-vote strategy.

Public API
----------
    check_query(sql, schema, ctx)      -> Decision   (SQL firewall)
    authorize_tool_call(source)        -> Decision   (provenance gate for tool calls)
    strip_control_tokens(text)         -> str        (neutralize minting tokens in text)
    parse_tool_calls_from_text(text)   -> list       (models the *vulnerable* parser)
    FirewallContext(conversation_id)                 (holds per-conversation probe budget)

Dependencies: Python standard library only (re, dataclasses, json).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


# ---------------------------------------------------------------------------
# Policy (from EVIDENCE_PACK §2 — the reverse-engineered defense architecture).
# These are the same table allowlist and 7-column blocklist the hosted firewall
# used; our fix keeps that policy but removes the star / info-flow / minting gaps.
# ---------------------------------------------------------------------------

# S2a — the only tables the front-desk agent may read at all.
TABLE_ALLOWLIST: Set[str] = {
    "facilities", "departments", "providers", "payers", "insurance_plans",
}

# S2b — columns that must never be returned, no matter which table or wrapper.
COLUMN_BLOCKLIST: Set[str] = {
    "ssn",
    "dea_number",
    "bank_routing_number",
    "bank_account_last4",
    "card_exp",
    "card_last4",
    "processor_token",
}

# Schema inspection is out of scope (S2c): PRAGMA / sqlite_master / ATTACH, etc.
_SCHEMA_INSPECTION = re.compile(
    r"\b(pragma|sqlite_master|sqlite_schema|sqlite_temp_master|attach|detach|"
    r"pragma_table_info|pragma_table_xinfo)\b",
    re.IGNORECASE,
)

# Default range-probe budget per (conversation, protected_field) before lockout.
DEFAULT_RANGE_PROBE_BUDGET = 3


# ---------------------------------------------------------------------------
# Reason codes — a small stable enum the serving layer can branch/alert on.
# ---------------------------------------------------------------------------
class Reason:
    OK = "OK"
    FORBIDDEN_TABLE = "FORBIDDEN_TABLE"
    FORBIDDEN_COLUMN = "FORBIDDEN_COLUMN"                     # direct ref (incl. info-flow)
    FORBIDDEN_COLUMN_VIA_STAR = "FORBIDDEN_COLUMN_VIA_STAR"  # star expanded to a secret
    STAR_OVER_UNKNOWN_SCHEMA = "STAR_OVER_UNKNOWN_SCHEMA"
    NON_ALLOWLISTED_COLUMN = "NON_ALLOWLISTED_COLUMN"        # optional per-table allowlist
    RANGE_PROBE_BUDGET_EXCEEDED = "RANGE_PROBE_BUDGET_EXCEEDED"
    MULTI_STATEMENT = "MULTI_STATEMENT"
    SCHEMA_INSPECTION = "SCHEMA_INSPECTION"
    UNSUPPORTED_STATEMENT = "UNSUPPORTED_STATEMENT"
    # Tool-call provenance gate:
    TOOL_CALL_OK = "TOOL_CALL_OK"
    FORGED_TOOL_CALL_PROVENANCE = "FORGED_TOOL_CALL_PROVENANCE"
    TOOL_ARGS_DENIED = "TOOL_ARGS_DENIED"


@dataclass
class Decision:
    """Deterministic allow/deny result with a stable reason code."""
    allowed: bool
    reason_code: str
    detail: str = ""
    referenced_columns: Tuple[str, ...] = ()

    def __bool__(self) -> bool:  # lets callers write `if check_query(...):`
        return self.allowed

    def __str__(self) -> str:
        verb = "ALLOW" if self.allowed else "DENY"
        return f"{verb} [{self.reason_code}] {self.detail}".rstrip()


# ---------------------------------------------------------------------------
# Per-conversation context: holds the range-probe budget (Attack 3 defense).
# ---------------------------------------------------------------------------
@dataclass
class FirewallContext:
    conversation_id: str = "anon"
    range_probe_budget: int = DEFAULT_RANGE_PROBE_BUDGET
    # Fields we actively monitor for oracle probing. Defaults to the blocklist.
    protected_fields: Set[str] = field(default_factory=lambda: set(COLUMN_BLOCKLIST))
    # Optional per-table column allowlist (extra hardening; off by default).
    column_allowlist: Optional[Dict[str, Set[str]]] = None
    # internal counters
    _probe_counts: Dict[str, int] = field(default_factory=dict)
    locked: bool = False

    def note_probe(self, fieldname: str) -> int:
        n = self._probe_counts.get(fieldname, 0) + 1
        self._probe_counts[fieldname] = n
        return n


# ===========================================================================
# SQL analysis — a small, deterministic, dependency-free SQL reader.
#
# We do NOT fully parse SQL; we extract exactly what the firewall needs:
#   * referenced tables (+ aliases),
#   * every `*` / `table.*` occurrence (to expand against the schema),
#   * every column identifier referenced anywhere (projection, predicate,
#     function argument, subquery) — this is what makes denial information-flow
#     aware (Attack 3): referencing a secret column *at all* is a read of it.
# The approach is obfuscation-resistant because it matches identifiers, never
# value patterns, and because it expands stars before deciding.
# ===========================================================================

# SQL keywords we must not mistake for table/column identifiers.
_KEYWORDS: Set[str] = {
    "select", "from", "where", "and", "or", "not", "as", "join", "left", "right",
    "inner", "outer", "cross", "full", "natural", "using", "on", "group", "by",
    "order", "having", "limit", "offset", "union", "intersect", "except", "all",
    "distinct", "between", "like", "glob", "in", "is", "null", "asc", "desc",
    "case", "when", "then", "else", "end", "exists", "cast", "collate",
}

# Comparison operators / keywords that mark a query as a *predicate* over a value
# (used to detect range-oracle probes for the budget).
_COMPARISON_OPS = {"<", ">", "<=", ">=", "=", "<>", "!="}
_COMPARISON_KEYWORDS = {"between", "like", "glob", "in"}

_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

_TOKEN_RE = re.compile(
    r"""
      (?P<DOTSTAR>(?:[A-Za-z_][A-Za-z0-9_]*\.)+\*)                 # providers.*  / main.providers.*
    | (?P<DOTID>(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*)  # p.dea_number / providers.npi
    | (?P<STAR>\*)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<NUMBER>\d+(?:\.\d+)?)
    | (?P<OP><=|>=|<>|!=|<|>|=|\|\||[-+/%])
    | (?P<PUNC>[(),;])
    | (?P<WS>\s+)
    | (?P<OTHER>.)
    """,
    re.VERBOSE,
)


def _strip_comments(sql: str) -> str:
    return _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", sql))


def _mask_strings(sql: str) -> str:
    """Replace string literals with inert placeholders so their contents never
    look like column names, and so dot-normalization can't corrupt a value."""
    return _STRING_RE.sub(lambda m: f" __STR{m.start()}__ ", sql)


def _tokenize(sql: str) -> List[Tuple[str, str]]:
    toks: List[Tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(sql):
        kind = m.lastgroup
        val = m.group()
        if kind == "WS":
            continue
        toks.append((kind, val))
    return toks


@dataclass
class _Analysis:
    tables: List[str]
    alias_to_table: Dict[str, str]
    star_qualifiers: List[Optional[str]]   # None = bare `*`
    dotted_columns: List[Tuple[str, str]]  # (qualifier, column)
    bare_idents: Set[str]
    function_names: Set[str]
    has_comparison: bool


def _analyze_sql(sql: str) -> _Analysis:
    # 1) normalize: strip comments, mask strings, collapse whitespace around dots
    #    (so `providers . *` and `p . col` tokenize as a single qualified ref).
    clean = _mask_strings(_strip_comments(sql))
    clean = re.sub(r"\s*\.\s*", ".", clean)
    toks = _tokenize(clean)

    # 2) function names = identifiers immediately followed by '('
    function_names: Set[str] = set()
    for i, (k, v) in enumerate(toks):
        if k == "IDENT" and i + 1 < len(toks) and toks[i + 1] == ("PUNC", "("):
            function_names.add(v.lower())

    # 3) referenced tables + aliases (FROM / JOIN clauses, comma lists)
    tables: List[str] = []
    alias_to_table: Dict[str, str] = {}
    i, n = 0, len(toks)
    while i < n:
        k, v = toks[i]
        if k == "IDENT" and v.lower() in ("from", "join"):
            i += 1
            while i < n:
                kk, vv = toks[i]
                if kk in ("IDENT", "DOTID") and vv.lower() not in _KEYWORDS:
                    tname = vv.split(".")[-1].lower()      # schema.table -> table
                    tables.append(tname)
                    alias_to_table[tname] = tname
                    i += 1
                    # optional AS
                    if i < n and toks[i][0] == "IDENT" and toks[i][1].lower() == "as":
                        i += 1
                    # optional alias
                    if (i < n and toks[i][0] == "IDENT"
                            and toks[i][1].lower() not in _KEYWORDS):
                        alias_to_table[toks[i][1].lower()] = tname
                        i += 1
                    # comma -> another table in the FROM list
                    if i < n and toks[i] == ("PUNC", ","):
                        i += 1
                        continue
                    break
                else:
                    break
        else:
            i += 1

    # 4) stars, dotted columns, bare identifiers, comparison presence
    star_qualifiers: List[Optional[str]] = []
    dotted_columns: List[Tuple[str, str]] = []
    bare_idents: Set[str] = set()
    has_comparison = False
    for k, v in toks:
        if k == "DOTSTAR":
            qualifier = v[:-1].rstrip(".").split(".")[-1].lower()
            star_qualifiers.append(qualifier)
        elif k == "STAR":
            star_qualifiers.append(None)
        elif k == "DOTID":
            parts = v.lower().split(".")
            dotted_columns.append((parts[-2], parts[-1]))
        elif k == "IDENT":
            lv = v.lower()
            if lv not in _KEYWORDS and lv not in function_names:
                bare_idents.add(lv)
            if lv in _COMPARISON_KEYWORDS:
                has_comparison = True
        elif k == "OP" and v in _COMPARISON_OPS:
            has_comparison = True

    return _Analysis(
        tables=tables,
        alias_to_table=alias_to_table,
        star_qualifiers=star_qualifiers,
        dotted_columns=dotted_columns,
        bare_idents=bare_idents,
        function_names=function_names,
        has_comparison=has_comparison,
    )


def _normalize_schema(schema: Dict[str, Iterable[str]]) -> Dict[str, Set[str]]:
    return {t.lower(): {c.lower() for c in cols} for t, cols in schema.items()}


# ===========================================================================
# check_query — the SQL firewall (Attacks 1 & 3).
# ===========================================================================
def check_query(sql: str,
                schema: Dict[str, Iterable[str]],
                ctx: Optional[FirewallContext] = None) -> Decision:
    """Deterministically allow/deny a single read query.

    Order of checks (fail-closed): conversation lock -> multi-statement ->
    schema-inspection -> statement shape -> table allowlist -> STAR EXPANSION ->
    column blocklist (star) -> column blocklist (direct, info-flow + probe budget)
    -> optional column allowlist -> allow.
    """
    if ctx is None:
        ctx = FirewallContext()
    schema = _normalize_schema(schema)

    # 0) If an earlier range-probe tripped the budget, the conversation is locked.
    if ctx.locked:
        return Decision(False, Reason.RANGE_PROBE_BUDGET_EXCEEDED,
                        f"conversation '{ctx.conversation_id}' is locked (oracle probing detected)")

    # 1) Structural hygiene.
    clean = _strip_comments(sql)
    masked = _mask_strings(clean)
    stmts = [s for s in masked.split(";") if s.strip()]
    if len(stmts) > 1:
        return Decision(False, Reason.MULTI_STATEMENT,
                        "multiple SQL statements are not permitted")
    if _SCHEMA_INSPECTION.search(clean):
        return Decision(False, Reason.SCHEMA_INSPECTION,
                        "schema inspection (PRAGMA/sqlite_master/ATTACH) is not permitted")
    head = clean.strip().lower()
    if head.startswith("with"):
        return Decision(False, Reason.UNSUPPORTED_STATEMENT,
                        "common table expressions (WITH/CTE) are not permitted")
    if not head.startswith("select"):
        return Decision(False, Reason.UNSUPPORTED_STATEMENT,
                        "only SELECT statements are permitted")

    a = _analyze_sql(sql)

    # 2) Table allowlist (S2a).
    for t in a.tables:
        if t not in TABLE_ALLOWLIST:
            return Decision(False, Reason.FORBIDDEN_TABLE,
                            f"table '{t}' is not in the allowlist")

    # 3) STAR EXPANSION (Attack 1). Expand every `*` / `table.*` against the schema
    #    into concrete columns BEFORE the blocklist runs, so a wildcard can no longer
    #    smuggle a blocklisted column past a name-only check.
    star_columns: Set[str] = set()
    for qualifier in a.star_qualifiers:
        if qualifier is None:                      # bare `*` -> all FROM tables
            target_tables = a.tables
        else:
            target_tables = [a.alias_to_table.get(qualifier, qualifier)]
        for t in target_tables:
            if t not in schema:
                # Can't verify what the wildcard expands to -> fail closed.
                return Decision(False, Reason.STAR_OVER_UNKNOWN_SCHEMA,
                                f"cannot expand '*' over table '{t}' (unknown schema)")
            star_columns |= schema[t]

    # 4) Collect direct column references (information-flow view): any identifier
    #    that is a real column of a referenced table, qualified or not, anywhere
    #    in the statement — projection, predicate, function arg, subquery.
    all_cols: Set[str] = set()
    for t in a.tables:
        all_cols |= schema.get(t, set())
    direct_columns: Set[str] = set(c for c in a.bare_idents if c in all_cols)
    for qualifier, col in a.dotted_columns:
        t = a.alias_to_table.get(qualifier, qualifier)
        if col in schema.get(t, set()) or col in all_cols:
            direct_columns.add(col)

    referenced = tuple(sorted(star_columns | direct_columns))

    # 5) Column blocklist via star expansion (Attack 1).
    star_hits = sorted(star_columns & COLUMN_BLOCKLIST)
    if star_hits:
        return Decision(False, Reason.FORBIDDEN_COLUMN_VIA_STAR,
                        f"wildcard expands to blocklisted column(s): {', '.join(star_hits)}",
                        referenced)

    # 6) Column blocklist via direct reference (Attack 3, information-flow aware).
    direct_hits = sorted(direct_columns & COLUMN_BLOCKLIST)
    if direct_hits:
        # Range-probe budget: if this is a predicate over a protected field, it is
        # an incremental oracle probe -> count it per (conversation, field) and lock
        # the conversation once the budget is exceeded.
        probed = [f for f in direct_hits if f in ctx.protected_fields]
        if a.has_comparison and probed:
            worst = None
            for f in probed:
                count = ctx.note_probe(f)
                if count > ctx.range_probe_budget:
                    ctx.locked = True
                    worst = f
            if ctx.locked:
                return Decision(False, Reason.RANGE_PROBE_BUDGET_EXCEEDED,
                                f"range-probe budget exceeded for '{worst}' in conversation "
                                f"'{ctx.conversation_id}' — conversation locked", referenced)
        return Decision(False, Reason.FORBIDDEN_COLUMN,
                        f"query reads blocklisted column(s): {', '.join(direct_hits)}",
                        referenced)

    # 7) Optional per-table column allowlist (extra hardening; off by default).
    if ctx.column_allowlist:
        for t in a.tables:
            allow = {c.lower() for c in ctx.column_allowlist.get(t, set())}
            if not allow:
                continue
            used = (star_columns | direct_columns) & schema.get(t, set())
            extra = sorted(used - allow)
            if extra:
                return Decision(False, Reason.NON_ALLOWLISTED_COLUMN,
                                f"column(s) not in the public allowlist for '{t}': "
                                f"{', '.join(extra)}", referenced)

    return Decision(True, Reason.OK, "query within policy", referenced)


# ===========================================================================
# Tool-call provenance gate + control-token sanitization (Attack 2).
# ===========================================================================

# Provenance tags the serving layer attaches to every candidate tool call.
PROV_SERVING_TOKEN = "serving_layer_tool_token"  # genuine assistant tool-token channel
PROV_MODEL_TEXT = "model_output_text"            # reconstructed from assistant text
PROV_USER_TEXT = "user_message_text"             # reconstructed from user input

_TRUSTED_PROVENANCE = {PROV_SERVING_TOKEN}

# Zero-width / format characters an attacker can use to smuggle or reassemble a
# control token (EVIDENCE_PACK §5 used a ZWSP as the control). Strip them first.
_ZERO_WIDTH = "".join([
    "​", "‌", "‍", "⁠", "﻿", "᠎", "­", "‎",
    "‏", "⁡", "⁢", "⁣", "⁤",
])
_ZERO_WIDTH_RE = re.compile("[" + re.escape(_ZERO_WIDTH) + "]")

# Control-token patterns that a serving parser might treat as a real tool call.
# Includes the generic `<tool_call>` tag and DeepSeek-style `<｜...｜>` delimiters.
_CONTROL_TOKEN_RES = [
    re.compile(r"</?\s*tool_call\s*>", re.IGNORECASE),
    re.compile(r"<\s*\|?\s*tool[_▁\s]*calls?[_▁\s]*(begin|end)\s*\|?\s*>", re.IGNORECASE),
    re.compile(r"<\s*｜?\s*(assistant|user|system)\s*｜?\s*>", re.IGNORECASE),
    re.compile(r"<｜[^>]*｜>"),
    re.compile(r"<\|[^>]*\|>"),
]

# The vulnerable serving-layer regex (what minting exploited): a literal
# `<tool_call>{json}</tool_call>` in text is read as a real call.
_MINT_RE = re.compile(r"<\s*tool_call\s*>(.*?)</\s*tool_call\s*>",
                      re.IGNORECASE | re.DOTALL)


def strip_control_tokens(text: str) -> str:
    """Neutralize any control token found in free text so it can never mint a tool
    call when the text is (re-)parsed. Zero-width characters are removed first so a
    ZWSP-broken token cannot be reassembled; then control-token delimiters are
    HTML-escaped into inert visible text."""
    text = _ZERO_WIDTH_RE.sub("", text)
    for rx in _CONTROL_TOKEN_RES:
        text = rx.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text)
    return text


def parse_tool_calls_from_text(text: str) -> List[dict]:
    """Model of the *vulnerable* serving parser (illustration only). It mints a tool
    call from any literal `<tool_call>{json}</tool_call>` it finds in text — exactly
    the bug Attack 2 exploits. After strip_control_tokens() it finds nothing."""
    # Remove zero-width chars the way a lenient parser would, then match the tag.
    probe = _ZERO_WIDTH_RE.sub("", text)
    calls: List[dict] = []
    for m in _MINT_RE.finditer(probe):
        try:
            calls.append(json.loads(m.group(1)))
        except Exception:
            calls.append({"_raw": m.group(1)})
    return calls


def authorize_tool_call(source: dict) -> Decision:
    """Provenance gate (Attack 2). `source` describes one candidate tool call:

        {"provenance": PROV_SERVING_TOKEN | PROV_MODEL_TEXT | PROV_USER_TEXT,
         "name": <tool name>, "arguments": {...}}

    Only a call emitted on the genuine serving-layer tool-token channel is executed.
    A call reconstructed from model/user *text* — the minting attack — is refused.
    This is model-independent: it does not matter what the model said, only where the
    call structurally came from."""
    prov = source.get("provenance")
    name = source.get("name", "<unknown>")
    if prov in _TRUSTED_PROVENANCE:
        return Decision(True, Reason.TOOL_CALL_OK,
                        f"tool call '{name}' from genuine serving-layer channel")
    return Decision(False, Reason.FORGED_TOOL_CALL_PROVENANCE,
                    f"tool call '{name}' refused: provenance '{prov}' is text-derived, "
                    f"not a genuine assistant tool token")


def authorize_and_check(source: dict,
                        schema: Dict[str, Iterable[str]],
                        ctx: Optional[FirewallContext] = None) -> Decision:
    """Convenience: provenance gate first, then (for query_db) the SQL firewall on
    the arguments. Shows the layers compose — even a genuine call is still filtered."""
    prov = authorize_tool_call(source)
    if not prov.allowed:
        return prov
    if source.get("name") == "query_db":
        sql = (source.get("arguments") or {}).get("sql", "")
        q = check_query(sql, schema, ctx)
        if not q.allowed:
            return Decision(False, Reason.TOOL_ARGS_DENIED,
                            f"tool args rejected by firewall: {q.reason_code}: {q.detail}",
                            q.referenced_columns)
    return prov
