#!/usr/bin/env python3
"""Write an engineer-facing impact summary for an SVD change.

Two layers, matching the pipeline's division of labor -- deterministic tools
handle volume; the LLM does judgment on bounded input:

  facts     -- computed here by diffing the introspection output. Always
               present, exact, and verifiable against `keelhaul count-registers`
               run by hand.
  narrative -- an LLM's prose reading of the SVD diff. Added only when a
               headless LLM CLI is available; the facts stand alone without it.

The narrative runs as a single shot with tools disabled: judgment on bounded
input, not an agent. Input is curated by the caller -- the .svd diff plus the
base/head introspection, a few KB. The multi-MB SVDs and the generated Rust
never reach the model.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys

# Headless LLM CLI used for the narrative layer. Its flags below are specific to
# this binary; swap both together if you retarget.
LLM = "claude"

SYSTEM = """You are a hardware verification engineer reviewing a change to a \
CMSIS-SVD memory-map specification on a pull request.

You receive a unified diff of the .svd file, and introspection output \
(peripheral inventory, register count, reset-value count) for the base and head \
revisions.

Write a short Markdown narrative for the reviewing engineer:

- What peripherals were added, removed, or renamed.
- What registers moved (give old -> new offsets), were added, or removed.
- What reset values changed (give old -> new).
- How the coverage footprint changed, using the introspection counts.

Rules: report only what the diff and counts actually show. Do not speculate \
about intent or downstream impact. A table of exact counts is generated \
separately and shown below your text -- do not reproduce it. If the diff is \
empty, say "No impact - the SVD is unchanged." and nothing else. No preamble, \
no headings above level 3."""


# --- deterministic layer -------------------------------------------------

def parse_ls_top(text):
    """ls-top output -> {peripheral: register count}."""
    out = {}
    for line in text.splitlines():
        # Rows are "NAME<spaces>COUNT"; skip the rubric and its dashed underline.
        m = re.fullmatch(r"(\S+)\s+(\d+)", line.strip())
        if m and not line.startswith("-"):
            out[m.group(1)] = int(m.group(2))
    return out


def parse_counts(text):
    """count-registers / count-reset-values -> (total, with_reset)."""
    total = with_reset = None
    for line in text.splitlines():
        if re.fullmatch(r"\d+", line.strip()):
            total = int(line.strip())
        m = re.match(r"(\d+)/(\d+)", line.strip())
        if m:
            with_reset, total = int(m.group(1)), int(m.group(2))
    return total, with_reset


def facts(base_text, head_text, diff_text):
    """The deterministic half. No model involved."""
    if not diff_text.strip():
        return "No impact — the SVD is unchanged.\n"

    base, head = parse_ls_top(base_text), parse_ls_top(head_text)
    b_total, b_reset = parse_counts(base_text)
    h_total, h_reset = parse_counts(head_text)

    def delta(new, old):
        if new is None or old is None:
            return str(new if new is not None else "?")
        d = new - old
        return f"{new} ({d:+d})" if d else str(new)

    lines = [
        "### Coverage footprint",
        "",
        "| | base | head |",
        "|---|---|---|",
        f"| Peripherals | {len(base) or '?'} | {delta(len(head), len(base))} |",
        f"| Registers | {b_total or '?'} | {delta(h_total, b_total)} |",
        f"| With testable reset value | {b_reset or '?'} | {delta(h_reset, b_reset)} |",
        "",
    ]

    added = sorted(set(head) - set(base))
    removed = sorted(set(base) - set(head))
    changed = sorted(p for p in set(base) & set(head) if base[p] != head[p])
    if added or removed or changed:
        lines += ["### Peripheral inventory", ""]
        lines += [f"- **added** `{p}` ({head[p]} registers)" for p in added]
        lines += [f"- **removed** `{p}` (was {base[p]} registers)" for p in removed]
        lines += [f"- `{p}` register count {base[p]} → {head[p]}" for p in changed]
        lines.append("")

    # Field-level edits the counts cannot show: an offset or reset value that
    # changed leaves the totals identical.
    edits = {
        "addressOffset": len(re.findall(r"^-.*<addressOffset>", diff_text, re.M)),
        "resetValue": len(re.findall(r"^-.*<resetValue>", diff_text, re.M)),
    }
    if any(edits.values()):
        lines += ["### Field edits in the diff", ""]
        lines += [f"- `<{f}>` changed on {n} register(s)" for f, n in edits.items() if n]
        lines.append("")

    return "\n".join(lines)


# --- judgment layer ------------------------------------------------------

def narrative(diff_text, base_text, head_text):
    """Prose via the headless LLM CLI. None when the CLI is unavailable."""
    if not shutil.which(LLM):
        return None

    prompt = (
        f"## SVD diff\n\n```diff\n{diff_text}\n```\n\n"
        f"## Introspection (base)\n\n```\n{base_text}\n```\n\n"
        f"## Introspection (head)\n\n```\n{head_text}\n```\n"
    )
    try:
        proc = subprocess.run(
            [LLM, "-p", "--output-format", "json",
             "--system-prompt", SYSTEM, "--allowed-tools", "", "--max-turns", "1"],
            input=prompt, capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"LLM invocation failed: {e}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"LLM exited {proc.returncode}: {proc.stderr[:500]}", file=sys.stderr)
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"LLM output not JSON: {proc.stdout[:500]}", file=sys.stderr)
        return None
    if payload.get("is_error"):
        print(f"LLM reported error: {payload.get('subtype')}", file=sys.stderr)
        return None
    return (payload.get("result") or "").strip() or None


# --- driver --------------------------------------------------------------

def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    diff_text, base_text, head_text = read(args.diff), read(args.base), read(args.head)

    prose = narrative(diff_text, base_text, head_text)
    if prose is None:
        prose = ("_LLM narrative skipped: no headless LLM CLI available. "
                 "The counts below are computed deterministically._")

    with open(args.out, "w") as f:
        f.write((prose + "\n\n" + facts(base_text, head_text, diff_text)).strip() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
