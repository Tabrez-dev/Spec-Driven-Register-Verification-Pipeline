#!/usr/bin/env python3
"""Checks that fail if the summary logic breaks. Run: python3 scripts/test_impact_summary.py"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from impact_summary import SYSTEM, facts, parse_ls_top  # noqa: E402

BASE = "== ls-top ==\nADC0 58\nUART0 10\n== count-registers ==\n68\n"
HEAD = "== ls-top ==\nADC0 58\nUART0 10\nUART8 12\n== count-registers ==\n80\n"


def test_facts_reports_added_peripheral_and_delta():
    out = facts(BASE, HEAD, "+ <peripheral>UART8</peripheral>")
    assert "**added** `UART8` (12 registers)" in out, out
    assert "80 (+12)" in out, out
    assert parse_ls_top(HEAD)["UART8"] == 12


def test_empty_diff_is_no_impact():
    assert facts(BASE, BASE, "").strip() == "No impact — the SVD is unchanged."


def test_reset_value_edit_surfaces_even_when_counts_match():
    diff = "-<resetValue>0x1</resetValue>\n+<resetValue>0x2</resetValue>"
    out = facts(BASE, BASE, diff)
    assert "`<resetValue>` changed on 1 register(s)" in out, out


def test_system_prompt_covers_the_four_asks():
    for phrase in ("peripherals", "offsets", "reset value", "coverage footprint", "No impact"):
        assert phrase in SYSTEM, phrase


def test_degrades_when_llm_absent():
    # Empty PATH -> shutil.which(LLM) is None -> deterministic facts only.
    with tempfile.TemporaryDirectory() as d:
        p = {}
        for n, txt in (("diff", "+ <peripheral>UART8</peripheral>"),
                       ("base", BASE), ("head", HEAD)):
            p[n] = os.path.join(d, n)
            open(p[n], "w").write(txt)
        out = os.path.join(d, "summary.md")
        env = dict(os.environ, PATH="")
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "impact_summary.py"),
             "--diff", p["diff"], "--base", p["base"], "--head", p["head"], "--out", out],
            env=env, capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        body = open(out).read()
        assert "skipped" in body and "| base | head |" in body, body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
