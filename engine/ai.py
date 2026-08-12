"""Turning a plain-English prompt into a formula, using Claude.

Optional: everything else in the app works without it. The user supplies their
own API key in the browser; it is held in the server's session memory for the
life of the process and is never written to disk, never saved into a mapping
profile, and never logged.
"""
from __future__ import annotations

import json
import re

MODEL = "claude-opus-5"

SYSTEM = """\
You convert a spreadsheet user's plain-English request into ONE formula for a \
data-transformation tool. You are given the columns available from three files.

The formula language:
- Reference a column in square brackets with its file prefix:
  [new.NTP] a column from New Data (the transaction)
  [master.UTP] a column from Master Data (the standard attributes)
  [donor.NTP] a column from Historical Data (used for gap-filled rows)
  [grid.DATE] the row's own reporting date
  [out.BRAND] a column already built earlier in the output
- Operators: + - * / % ** and comparisons. '+' joins values when either is text.
- Functions: CONCAT, IF, IFERROR, COALESCE, RANDBETWEEN, RAND, ROUND, ROUNDUP,
  ROUNDDOWN, INT, ABS, SUM, AVERAGE, MIN, MAX, UPPER, LOWER, PROPER, TRIM, LEN,
  LEFT, RIGHT, PAD, TEXT, VALUE, YEAR, MONTH, DAY, ISBLANK, NOT.
- There is no VLOOKUP, no cell references, and no ranges. Every value comes from
  a column on the same row.

Rules:
- Use ONLY column names from the provided list, spelled exactly, with a prefix.
- If the request cannot be expressed, say so instead of inventing a column.
- Prefer the simplest formula that answers the request.

Reply with JSON only, no prose and no code fences:
{"formula": "<the formula, or empty string if impossible>",
 "explanation": "<one short sentence>",
 "confidence": "high" | "medium" | "low"}\
"""


class AIError(RuntimeError):
    pass


def available() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _client(api_key: str):
    try:
        import anthropic
    except ImportError as e:
        raise AIError("The 'anthropic' package is not installed on this machine.") from e
    if not api_key:
        raise AIError("No API key has been set.")
    return anthropic.Anthropic(api_key=api_key)


def _columns_block(columns: dict[str, list[str]]) -> str:
    lines = []
    for ns, label in (("new", "New Data"), ("master", "Master Data"),
                      ("donor", "Template + Past Data"), ("out", "Output columns so far")):
        cols = columns.get(ns) or []
        if cols:
            lines.append(f"{label} (prefix '{ns}.'):")
            lines.extend(f"  [{ns}.{c}]" for c in cols)
    lines.append("Grid: [grid.DATE]")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise AIError("The model did not return a usable answer. Try rewording the prompt.")


def suggest_formula(api_key: str, prompt: str, columns: dict[str, list[str]],
                    target_column: str | None = None,
                    samples: dict | None = None) -> dict:
    """Ask Claude for a formula. Returns {formula, explanation, confidence}."""
    if not str(prompt or "").strip():
        raise AIError("Describe what the column should contain.")

    parts = [f"Columns available:\n{_columns_block(columns)}"]
    if target_column:
        parts.append(f"\nThe formula fills the output column: {target_column}")
    if samples:
        shown = {k: v for k, v in list(samples.items())[:40]}
        parts.append("\nExample values from the first row:\n" +
                     "\n".join(f"  [{k}] = {v!r}" for k, v in shown.items()))
    parts.append(f"\nRequest: {prompt.strip()}")

    client = _client(api_key)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
    except Exception as e:  # surface the provider's message, minus the key
        raise AIError(f"{type(e).__name__}: {e}") from e

    if getattr(resp, "stop_reason", None) == "refusal":
        raise AIError("The model declined this request.")

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    data = _extract_json(text)
    formula_text = str(data.get("formula") or "").strip()
    if not formula_text:
        raise AIError(data.get("explanation")
                      or "That cannot be expressed as a formula over these columns.")
    return {
        "formula": formula_text,
        "explanation": str(data.get("explanation") or ""),
        "confidence": str(data.get("confidence") or "medium"),
        "model": MODEL,
    }


def check_key(api_key: str) -> dict:
    """Cheap round-trip so the user finds out immediately if the key is wrong."""
    client = _client(api_key)
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the word OK."}],
        )
    except Exception as e:
        raise AIError(f"{type(e).__name__}: {e}") from e
    return {"model": MODEL, "ok": True,
            "reply": "".join(b.text for b in resp.content
                             if getattr(b, "type", None) == "text").strip()[:40]}
