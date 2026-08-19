#!/usr/bin/env python3
"""Self-test for climits: the pace line, the gate policy, subagent accounting, access
states, and the optional shared-account layer.

Requires neither network nor a live account: the state directory is replaced with a
temporary one (`CLIMITS_STATE_DIR`), the account with an environment variable
(`CLIMITS_ACCOUNT`), and snapshots and history are built by hand.

    python3 tests/selftest.py        # 0 — everything matched, 1 — there are failures
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location

WINDOW = "seven_day"
TOTAL = 7 * 86400
NORM_PPH = 100.0 / (TOTAL / 3600.0)          # 0.595 pp/h — speed of the nominal line
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, title: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   {title}")
        return
    FAILURES.append(title)
    print(f"  FAIL {title}" + (f"\n         {detail}" if detail else ""))


def http_error(code: int, error_code: str | None,
               message: str = "denied") -> "urllib.error.HTTPError":
    """An API refusal exactly as it arrives: the code plus a body with
    `error.details.error_code`, which is how climits tells a closed subscription
    apart from other 401/403 answers."""
    body = {"error": {"message": message, "details": {"error_code": error_code}}}
    return urllib.error.HTTPError(
        "https://api.anthropic.com/api/oauth/usage", code, message, {},
        io.BytesIO(json.dumps(body).encode("utf-8")))


def load_climits():
    path = os.path.join(ROOT, "bin", "climits")
    spec = spec_from_file_location("climits", path,
                                   loader=SourceFileLoader("climits", path))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def budget(climits, overrides: dict | None = None, foreign: dict | None = None) -> dict:
    """A config "as if from a file", assembled in memory: defaults plus overrides.

    Layer 2 is enabled here by default (it ships disabled) — most scenarios below
    exist precisely to exercise it."""
    defaults = dict(climits.DEFAULTS)
    defaults.update(overrides or {})
    defaults["foreign"] = {"enabled": True, **(foreign or {})}
    return {"defaults": defaults, "accounts": {}}


def snapshot(now: float, used: float, seconds_left: int) -> dict:
    return {"account": "test", "fetchedAt": int(now), "apiFetchedAt": int(now),
            "source": "api",
            "windows": {WINDOW: {"used": used, "resets_at": int(now + seconds_left)}}}


def write_history(climits, samples: list[dict]) -> None:
    """samples: [{"t": ts, "act": ts, "used": %, "reset": ts|None}, ...]"""
    path = climits.history_path("test")
    with open(path, "w", encoding="utf-8") as fh:
        for item in samples:
            fh.write(json.dumps({
                "t": int(item["t"]), "act": int(item["act"]),
                "w": {WINDOW: {"u": item["used"], "r": item.get("reset")}},
            }) + "\n")


def clean_series(now: float, values: list[float], gap: int, reset: int | None) -> list[dict]:
    """A run of points sharing one `reset`, with activity strictly before the start of
    each interval — that is, every interval between them is clean (we were idle)."""
    start = now - gap * len(values)
    return [{"t": start + gap * i, "act": start + gap * max(i - 1, 0),
             "used": value, "reset": reset} for i, value in enumerate(values)]


def assistant(message_id: str, output: int, input: int, creation: int, read: int) -> dict:
    """One model reply inside a subagent transcript (`agent_transcript_path` format)."""
    return {"type": "assistant", "message": {
        "id": message_id, "role": "assistant", "model": "test-model",
        "usage": {"input_tokens": input, "output_tokens": output,
                  "cache_creation_input_tokens": creation,
                  "cache_read_input_tokens": read}}}


def write_transcript(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def write_budget(config_dir: str, defaults: dict) -> None:
    """A config fixture instead of the personal ~/.claude/limits-budget.json: the gate
    looks in $CLAUDE_CONFIG_DIR first, so swapping the directory detaches the run from
    the machine it happens to be on."""
    with open(os.path.join(config_dir, "limits-budget.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "defaults": defaults, "accounts": {}}, fh)


def run_gate(climits, payload: dict) -> tuple[dict, dict]:
    """A full hook call: stdin -> cmd_gate -> (what went out, last line of gate.log).

    Needs no network: the snapshot in the state directory is fresh, so probe stays
    inside its TTL."""
    import contextlib
    stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps(payload))
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            climits.cmd_gate([])
    finally:
        sys.stdin = stdin
    with open(os.path.join(os.environ["CLIMITS_STATE_DIR"], "gate.log"),
              encoding="utf-8") as fh:
        record = json.loads(fh.readlines()[-1])
    return json.loads(buffer.getvalue() or "{}"), record


def row_for(climits, snap: dict, cfg: dict, headless: bool = False) -> dict:
    report = climits.evaluate(snap, cfg, "test", headless)
    return next(r for r in report["windows"] if r["window"] == WINDOW)


def main() -> int:
    climits = load_climits()
    state = tempfile.mkdtemp(prefix="climits-selftest-")
    os.environ["CLIMITS_STATE_DIR"] = state
    os.environ["CLIMITS_ACCOUNT"] = "test"
    now = time.time()
    half = TOTAL // 2                       # half of the weekly window still ahead
    reset_at = int(now + half)
    burst = 120 * 60 / TOTAL * 100          # burst_minutes = 120 -> 1.19 pp
    # As in a real config: the head start given in time, the pace as a placeholder
    base = {"pace_factor": {"interactive": 0.9, "headless": 0.7},
            "burst_minutes": {"five_hour": 10, "seven_day": 120}}
    cfg = budget(climits, base)

    try:
        print("0. The optional layer 2 module is truly optional")
        climits._FOREIGN_CACHE.clear()
        os.environ["CLIMITS_FOREIGN"] = os.path.join(state, "no-such-module.py")
        write_history(climits, clean_series(now, [24.0, 26.0, 28.0, 30.0], 6 * 3600,
                                            reset_at))
        row = row_for(climits, snapshot(now, 30.0, half), cfg)
        check(climits.foreign_module() is None,
              "a missing lib/foreign.py resolves to None, not an error")
        check(row["foreign_measure"] is None and row["foreign_reserve_pp"] == 0.0
              and row["pace_source"] == "config",
              "without the module the core falls back to the configured pace",
              json.dumps(row["foreign_measure"]))
        climits._FOREIGN_CACHE.clear()
        os.environ.pop("CLIMITS_FOREIGN")
        check(climits.foreign_module() is not None,
              "the module next to the core does load")

        print("1. No history — the configured pace applies, layer 2 stays quiet")
        write_history(climits, [])
        row = row_for(climits, snapshot(now, 30.0, half), cfg)
        # The line is quadratic: the slope grows from 0.9 nominal at the start of the
        # window to 1.1 at the end, so at half the window 0.9*50 + 0.1*25 = 47.5 pp
        # are allowed, plus burst.
        line_cfg = 100.0 * (0.9 * 0.5 + 0.1 * 0.25) + burst
        check(row["foreign_measure"] is None and row["pace_source"] == "config",
              "no measurement of other people's load", json.dumps(row["foreign_measure"]))
        check(abs(row["allowed_own"] - line_cfg) < 0.05,
              f"allowance follows the configured line ({line_cfg:.2f} pp)",
              f"got {row['allowed_own']:.2f}")

        print("2. Nobody else is spending — we take the full nominal pace, no reserve")
        write_history(climits, clean_series(now, [30.0] * 4, 6 * 3600, reset_at))
        row = row_for(climits, snapshot(now, 30.0, half), cfg)
        line_full = 50.0 + burst
        check(row["foreign_measure"] is not None and row["pace_source"] == "measured",
              "the measurement happened and replaced pace_factor")
        check(row["foreign_pph"] == 0.0 and row["foreign_reserve_pp"] == 0.0,
              "zero foreign rate -> zero reserve",
              f"{row['foreign_pph']} pp/h, reserve {row['foreign_reserve_pp']}")
        check(abs(row["allowed_own"] - line_full) < 0.05,
              f"allowance sits exactly on the pace line ({line_full:.2f} pp)",
              f"got {row['allowed_own']:.2f}")
        check(row["allowed_own"] > line_cfg,
              "with nobody else around, more is allowed than under the 90% placeholder")

        print("3. Other people are active — their forecast is reserved, the cap is trimmed")
        # 3 clean intervals of 6h, growing 2 pp each: minus slack 0.5 that is 4.5 pp
        # over 18h, i.e. 0.25 pp/h (42% of nominal)
        write_history(climits, clean_series(now, [24.0, 26.0, 28.0, 30.0], 6 * 3600,
                                            reset_at))
        row = row_for(climits, snapshot(now, 30.0, half), cfg)
        expected_rate = (2.0 - 0.5) * 3 / 18.0
        check(abs(row["foreign_pph"] - expected_rate) < 1e-9,
              f"foreign rate {expected_rate:.3f} pp/h "
              f"({expected_rate/NORM_PPH:.0%} of nominal)",
              f"got {row['foreign_pph']:.4f}")
        room = (100.0 - 5.0) - 30.0
        expected_reserve = min(expected_rate / 3600.0 * half, room * 0.75)
        check(abs(row["foreign_reserve_pp"] - expected_reserve) < 0.05,
              f"reserved for others {expected_reserve:.1f} pp",
              f"got {row['foreign_reserve_pp']:.2f}")
        check(row["cap_own"] < 95.0 and row["cap_own"] >= 30.0,
              "the cap is trimmed, but never below what is already spent",
              f"cap {row['cap_own']:.2f}")

        print("4. Layer 2 can only slow us down — it never lifts the line")
        for used, left in ((5.0, half), (30.0, half), (60.0, TOTAL // 10)):
            row = row_for(climits, snapshot(now, used, left), cfg)
            line = 1.0 * (TOTAL - left) / TOTAL * 100.0 + burst
            # 0.01 pp tolerance — a fraction of a second of line growth between the
            # snapshot and the computation
            check(row["allowed_own"] <= line + 0.01,
                  f"used={used}%, {climits.humanize(left)} left: allowed "
                  f"{row['allowed_own']:.1f}% <= line {line:.1f}%")

        print("5. The cap bites where the line no longer binds (end of the window)")
        tail = TOTAL // 10                  # 90% of the window elapsed, idle, banked
        write_history(climits, clean_series(now, [20.0, 26.0, 32.0, 38.0], 6 * 3600,
                                            int(now + tail)))
        row = row_for(climits, snapshot(now, 40.0, tail), cfg)
        line_tail = 90.0 + burst
        check(row["cap_own"] < line_tail and abs(row["allowed_own"] - row["cap_own"]) < 1e-9,
              "the allowance is set by the cap, not by the line",
              f"cap {row['cap_own']:.2f}, line {line_tail:.2f}, "
              f"allowed {row['allowed_own']:.2f}")
        check(row["allowed_own"] >= 40.0,
              "the reserve never manufactures an overrun (reserve_cap_share bounds it)",
              f"allowed {row['allowed_own']:.2f} against a spend of 40.0")
        auto = row_for(climits, snapshot(now, 40.0, tail), cfg, headless=True)
        check(auto["allowed_own"] < row["allowed_own"],
              "an autonomous run cedes more than manual work does",
              f"headless {auto['allowed_own']:.2f} vs interactive {row['allowed_own']:.2f}")

        print("6. Our own activity inside an interval voids the measurement")
        dirty = clean_series(now, [24.0, 26.0, 28.0, 30.0], 6 * 3600, reset_at)
        for item in dirty[1:]:
            item["act"] = item["t"] - 60    # we worked a minute before the point
        write_history(climits, dirty)
        row = row_for(climits, snapshot(now, 30.0, half), cfg)
        check(row["foreign_measure"] is None,
              "dirty intervals are not counted", json.dumps(row["foreign_measure"]))
        boundary = clean_series(now, [24.0, 26.0, 28.0, 30.0], 6 * 3600, reset_at)
        for prev, cur in zip(boundary, boundary[1:]):
            cur["act"] = prev["t"]          # stamp exactly on the border — still clean
        write_history(climits, boundary)
        row = row_for(climits, snapshot(now, 30.0, half), cfg)
        check(row["foreign_measure"] is not None,
              "an activity stamp exactly on the border does not spoil cleanliness")

        print("6a. Legacy points (written before activity stamps) are not counted")
        legacy = clean_series(now, [24.0, 26.0, 28.0, 30.0], 6 * 3600, reset_at)
        with open(climits.history_path("test"), "w", encoding="utf-8") as fh:
            for item in legacy:
                fh.write(json.dumps({"t": int(item["t"]),
                                     "w": {WINDOW: item["used"]}}) + "\n")
        row = row_for(climits, snapshot(now, 30.0, half), cfg)
        check(row["foreign_measure"] is None,
              "history written before layer 2 does not fake a measurement",
              json.dumps(row["foreign_measure"]))

        print("7. A window reset is not mistaken for spend")
        reset_pair = clean_series(now, [10.0, 40.0], 6 * 3600, reset_at)
        reset_pair[0]["reset"] = reset_at - TOTAL      # a different window before the reset
        write_history(climits, reset_pair)
        row = row_for(climits, snapshot(now, 40.0, half), cfg)
        check(row["foreign_measure"] is None,
              "resets_at changed -> the interval is skipped",
              json.dumps(row["foreign_measure"]))
        drop = clean_series(now, [40.0, 10.0, 12.0], 6 * 3600, None)
        write_history(climits, drop)
        row = row_for(climits, snapshot(now, 12.0, half), cfg)
        measure = row["foreign_measure"]
        check(measure is not None and measure["intervals"] == 1,
              "a counter drop with no known resets_at skips only that interval",
              json.dumps(measure))

        print("8. Short intervals are discarded (the counter moves in whole percent)")
        write_history(climits, clean_series(now, [30.0] * 12, 20 * 60, reset_at))
        row = row_for(climits, snapshot(now, 30.0, half), cfg)
        check(row["foreign_measure"] is None,
              "20-minute intervals are below min_gap_seconds for 7d (2h)",
              json.dumps(row["foreign_measure"]))
        row = row_for(climits, snapshot(now, 30.0, half),
                      budget(climits, base,
                             {"min_gap_seconds": 600, "min_total_seconds": 3600}))
        check(row["foreign_measure"] is not None,
              "with min_gap relaxed the same data is accepted")

        print("9. The wait is set by the slowest constraint")
        write_history(climits, clean_series(now, [24.0, 26.0, 28.0, 30.0], 6 * 3600,
                                            reset_at))
        row = row_for(climits, snapshot(now, 90.0, half), cfg)
        line = 50.0 + burst
        by_line = (90.0 - line) / (100.0 / TOTAL)
        check(row["verdict"] == "exceed" and row["wait_seconds"] is not None,
              "the overrun is seen and the catch-up time computed",
              json.dumps(row["wait_seconds"]))
        check(row["wait_seconds"] >= by_line - 5,
              "we wait at least as long as the pace line needs",
              f"{row['wait_seconds']} against {by_line:.0f}s")
        check(row["wait_seconds"] <= half,
              "waiting longer than the reset itself is pointless",
              f"{row['wait_seconds']} against {half}s")
        over = row_for(climits, snapshot(now, 99.0, half),
                       budget(climits, base, {"enabled": False}))
        check(over["wait_seconds"] >= half - 5,
              "spend above the cap is only cleared by the window reset",
              f"{over['wait_seconds']} against {half}s")

        print("10. The layer 2 switch restores the previous behaviour")
        write_history(climits, clean_series(now, [24.0, 26.0, 28.0, 30.0], 6 * 3600,
                                            reset_at))
        row = row_for(climits, snapshot(now, 30.0, half),
                      budget(climits, base, {"enabled": False}))
        check(row["foreign_measure"] is None and row["pace_source"] == "config"
              and abs(row["allowed_own"] - line_cfg) < 0.05,
              "foreign.enabled = false -> the line follows the configured pace",
              f"allowed {row['allowed_own']:.2f}")

        print("11. The question is asked only where a question is possible at all")
        # Regression: `_decide` did not know the event, so the right to ask was spent
        # on UserPromptSubmit (which runs first on every prompt), where an answer with
        # permissionDecision is invalid and silently dropped. As a result PreToolUse,
        # the one place the question would have shown, always got warn-throttled.
        write_history(climits, [])
        deep = snapshot(now, 99.0, half)      # a long overrun: a pause will not help
        ask_cfg = budget(climits, dict(base, enforce=True))
        report = climits.evaluate(deep, ask_cfg, "test", False)
        prompt = climits._decide(report, ask_cfg, False, 0, "test", True,
                                 event="UserPromptSubmit")
        check(prompt["action"] != "ask",
              "no question on UserPromptSubmit (it cannot be shown there)",
              f"got {prompt['action']}")

        pre = climits._decide(report, ask_cfg, False, 0, "test", True, event="PreToolUse")
        check(pre["action"] == "ask",
              "the right to ask was not eaten — PreToolUse gets it",
              f"got {pre['action']}")
        check(pre["output"].get("hookSpecificOutput", {}).get("permissionDecision") == "ask",
              "the PreToolUse answer carries permissionDecision=ask",
              json.dumps(pre["output"], ensure_ascii=False))

        again = climits._decide(report, ask_cfg, False, 0, "test", True, event="PreToolUse")
        check(again["action"] == "warn-throttled",
              "a second question in a row is held back by the cooldown",
              f"got {again['action']}")

        print("12. The warning threshold is given in time, not in points")
        # Regression: soft_margin_pp is one number for every window, but the lines
        # differ in steepness (20 pp/h for 5h against 0.595 for 7d). 10 pp is half an
        # hour of slack on the five-hour window and 16.8h on the weekly one, so "!"
        # was lit on 7d almost always: a spend of 3% against 8% allowed (8+ hours of
        # slack) already counted as "approaching the line".
        write_history(climits, [])
        elapsed = TOTAL * 0.07                  # 7% of the week elapsed
        left = int(TOTAL - elapsed)
        minutes_cfg = budget(climits, dict(base, pace_factor={"interactive": 1.0,
                                                              "headless": 1.0}))
        minutes_cfg["defaults"].pop("soft_margin_pp", None)
        minutes_cfg["defaults"]["soft_margin_minutes"] = {"five_hour": 30,
                                                          "seven_day": 120}
        row = row_for(climits, snapshot(now, 3.0, left), minutes_cfg)
        margin_pp = 120 * 60 * (100.0 / TOTAL)  # 2h of line growth = 1.19 pp
        check(row.get("soft_margin_pp") is not None
              and abs(row["soft_margin_pp"] - margin_pp) < 0.01,
              f"120 minutes converted into {margin_pp:.2f} pp at the 7d nominal rate",
              f"got {row.get('soft_margin_pp')}")
        check(row["verdict"] == "ok",
              "8+ hours of slack against a 2h threshold — no warning",
              f"verdict {row['verdict']}, free {row['headroom']:.2f} pp, "
              f"slack {climits.humanize(row['slack_seconds'])}")

        near = row_for(climits, snapshot(now, 100.0 * 0.07 + 0.5, left), minutes_cfg)
        check(near["verdict"] == "soft",
              "less than two hours of slack — the warning turns on",
              f"verdict {near['verdict']}, free {near['headroom']:.2f} pp, "
              f"slack {climits.humanize(near['slack_seconds'])}")

        legacy = row_for(climits, snapshot(now, 3.0, left),
                         budget(climits, dict(base, soft_margin_pp=10.0)))
        check(legacy["verdict"] == "soft" and abs(legacy["soft_margin_pp"] - 10.0) < 1e-9,
              "an explicit soft_margin_pp still works (previous behaviour)",
              f"verdict {legacy['verdict']}, threshold {legacy.get('soft_margin_pp')}")

        print("13. Subagent spend comes from the transcript; stream duplicates do not double it")
        fixture = os.path.join(state, "agent-fixture.jsonl")
        write_transcript(fixture, [
            {"type": "user", "message": {"role": "user"}},
            assistant("msg_1", output=4, input=10, creation=11232, read=0),
            assistant("msg_1", output=157, input=10, creation=11232, read=0),
            assistant("msg_2", output=2, input=8, creation=716, read=11232),
            dict(assistant("msg_3", output=999, input=999, creation=999, read=999),
                 isApiErrorMessage=True),
        ])
        totals = climits.agent_usage_from_transcript(fixture)
        check(totals.get("output") == 159 and totals.get("input") == 18,
              "one record per message.id, the final one wins (output 4 -> 157)",
              json.dumps(totals))
        check(totals.get("cache_creation") == 11948 and totals.get("cache_read") == 11232,
              "cache writes and cache reads are counted separately", json.dumps(totals))
        check(totals.get("messages") == 2 and totals.get("models") == ["test-model"],
              "an API error reply is not counted as spend", json.dumps(totals))
        check(climits.agent_usage_from_transcript(os.path.join(state, "no-such-file")) == {},
              "a missing transcript does not break the hook")

        print("14. On SubagentStop the gate only counts and decides nothing")
        os.environ["CLAUDE_CONFIG_DIR"] = state          # config fixture, not the personal one
        write_budget(state, {"enforce": True, "pace_factor": {"interactive": 1.0,
                                                              "headless": 1.0},
                             "burst_minutes": {"five_hour": 10, "seven_day": 120},
                             "soft_margin_minutes": {"five_hour": 30, "seven_day": 120},
                             "warn_cooldown_minutes": 5})
        climits._atomic_write(climits.snapshot_path("test"), snapshot(now, 3.0, left))
        out, record = run_gate(climits, {"hook_event_name": "SubagentStop",
                                         "agent_id": "a1", "agent_type": "explore",
                                         "session_id": "s1",
                                         "agent_transcript_path": fixture})
        check(out == {} and record["action"] == "agent-usage",
              "no decision — the agent has already finished",
              f"{out} / {record['action']}")
        check((record.get("agent_tokens") or {}).get("output") == 159
              and record.get("agent") == "a1" and record.get("agent_type") == "explore",
              "both the agent and its spend reached gate.log",
              json.dumps(record.get("agent_tokens")))
        stored = climits.agents_read("test")
        check(len(stored) == 1 and stored[0]["output"] == 159,
              "the record landed in agents.jsonl", json.dumps(stored))
        run_gate(climits, {"hook_event_name": "SubagentStop", "agent_id": "a1",
                           "agent_type": "explore", "session_id": "s1",
                           "agent_transcript_path": fixture})
        stored = climits.agents_read("test")
        check(len(stored) == 1,
              "a repeated SubagentStop for the same agent does not double the spend",
              json.dumps(stored))

        print("15. The warning is muted by a cooldown (the gate runs on every tool)")
        climits._atomic_write(climits.snapshot_path("test"),
                              snapshot(now, 100.0 * 0.07 + 0.5, left))
        first_out, first = run_gate(climits, {"hook_event_name": "PreToolUse",
                                              "tool_name": "Read", "session_id": "s1"})
        second_out, second = run_gate(climits, {"hook_event_name": "PreToolUse",
                                                "tool_name": "Read", "session_id": "s1",
                                                "agent_id": "a2",
                                                "agent_type": "explore"})
        check(first["verdict"] == "soft" and first_out.get("systemMessage")
              and first["muted"] is False,
              "the first warning gets through", json.dumps(first_out, ensure_ascii=False))
        check(second_out == {} and second["muted"] is True
              and second["verdict"] == "soft",
              "the second in a row is silent, but the log keeps the same verdict",
              json.dumps(second_out, ensure_ascii=False))
        check(second.get("agent") == "a2",
              "a call from inside a subagent is distinguishable in the log by agent_id",
              json.dumps(second.get("agent")))

        print("16. A per-model window gates only a session running that model")
        write_history(climits, [])
        scoped = {"account": "test", "fetchedAt": int(now), "apiFetchedAt": int(now),
                  "source": "api", "windows": {
                      WINDOW: {"used": 3.0, "resets_at": int(now + left)},
                      "weekly_fable": {"used": 99.0, "model": "Fable",
                                       "resets_at": int(now + left)}}}
        opus = climits.evaluate(scoped, cfg, "test", False, ["claude-opus-5"])
        fable = climits.evaluate(scoped, cfg, "test", False, ["claude-fable-5"])
        unknown = climits.evaluate(scoped, cfg, "test", False, [])
        always = climits.evaluate(scoped, budget(climits, dict(base,
                                  scoped_windows="always")), "test", False,
                                  ["claude-opus-5"])
        row_fable = next(r for r in opus["windows"] if r["window"] == "weekly_fable")
        check(opus["verdict"] == "ok" and row_fable["gates"] is False
              and row_fable["verdict"] == "exceed",
              "on Opus the Fable window is over the line but does not spoil the verdict",
              f"{opus['verdict']}, gates={row_fable['gates']}")
        check(fable["verdict"] == "exceed"
              and fable["tightest"]["window"] == "weekly_fable",
              "on Fable the same window gates as before", fable["verdict"])
        check(unknown["verdict"] == "ok",
              "model unknown -> a scoped window does not gate (a false alarm costs more)",
              unknown["verdict"])
        check(always["verdict"] == "exceed",
              "scoped_windows=always restores the blunt behaviour", always["verdict"])
        check(climits.model_matches("Fable", "claude-fable-5")
              and not climits.model_matches("Fable", "claude-opus-5"),
              "the API model name is matched against the id from the transcript")

        legacy_snap = {"account": "test", "fetchedAt": int(now), "apiFetchedAt": int(now),
                       "source": "api", "windows": {
                           WINDOW: {"used": 3.0, "resets_at": int(now + left)},
                           "weekly_fable": {"used": 99.0,
                                            "resets_at": int(now + left)}}}
        row_legacy = next(r for r in climits.evaluate(legacy_snap, cfg, "test", False,
                                                      ["claude-opus-5"])["windows"]
                          if r["window"] == "weekly_fable")
        check(row_legacy["gates"] is False,
              "a snapshot without the `model` field is parsed by the window name")

        print("17. EVERY window in trouble goes out, not only the tightest")
        both = {"account": "test", "fetchedAt": int(now), "apiFetchedAt": int(now),
                "source": "api", "windows": {
                    "five_hour": {"used": 90.0,
                                  "resets_at": int(now + 5 * 3600 * 0.5)},
                    WINDOW: {"used": 90.0, "resets_at": int(now + left)}}}
        report = climits.evaluate(both, cfg, "test", False, [])
        names = [r["window"] for r in report["problems"]]
        check(names == ["five_hour", WINDOW] or names == [WINDOW, "five_hour"],
              "both windows are in problems", json.dumps(names))
        decision = climits._decide(report, budget(climits, dict(base, enforce=True)),
                                   True, 0, "test", False, event="PreToolUse")
        text = json.dumps(decision["output"], ensure_ascii=False)
        check("five_hour" in text and WINDOW in text,
              "the gate message names both windows", text)
        waits = [r["wait_seconds"] for r in report["problems"] if r["wait_seconds"]]
        check(decision.get("wait_seconds") in (None, 0)
              or decision["wait_seconds"] >= max(waits) or decision["action"] == "deny",
              "the pause is no shorter than the slowest window", f"{decision['action']} "
              f"{decision.get('wait_seconds')} against {waits}")

        print("18. The tightest window is picked by TIME, not by points")
        mixed = {"account": "test", "fetchedAt": int(now), "apiFetchedAt": int(now),
                 "source": "api", "windows": {
                     "five_hour": {"used": 43.0,
                                   "resets_at": int(now + 5 * 3600 * 0.5)},
                     WINDOW: {"used": 45.0, "resets_at": int(now + half)}}}
        mixed_report = climits.evaluate(mixed, cfg, "test", False, [])
        by_pp = min(mixed_report["windows"], key=lambda r: r["headroom"])["window"]
        check(mixed_report["tightest"]["window"] == "five_hour",
              "the tightest is the five-hour window (minutes of slack), not the weekly one",
              f"named tightest: {mixed_report['tightest']['window']}, "
              f"by points it would have been {by_pp}")

        print("19. The session model is read from the tail of the transcript")
        session_log = os.path.join(state, "session.jsonl")
        write_transcript(session_log, [
            {"type": "user", "message": {"role": "user"}},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5"}},
        ])
        found = climits.transcript_models(session_log)
        check(found == ["claude-opus-5"], "the model was read from the transcript",
              json.dumps(found))
        check(climits.transcript_models(os.path.join(state, "no-such-file")) == [],
              "a missing transcript does not break the hook")
        got = climits.session_models("test", {"session_id": "s9",
                                              "transcript_path": session_log})
        recent = climits.models_recent("test")
        check(got == ["claude-opus-5"] and "claude-opus-5" in recent,
              "the session model was remembered in the shared file", json.dumps(got))
        # A wave of subagents may run a different model than the main loop and spend
        # exactly that model's scoped window — SubagentStop (step 14) must register it.
        check("test-model" in recent,
              "the subagent's model also landed in the session models", json.dumps(recent))

        # Why this matters: "no access", "login expired" and "the API is silent" are
        # cured by three different actions (pay / start claude in that profile / wait),
        # and before the states were split all three looked identical in the table.
        print("20. An API refusal is classified by cause, not by one string")
        creds_real, urlopen_real = climits.oauth_credentials, urllib.request.urlopen
        try:
            def probe_with(error, expired: bool):
                climits.oauth_credentials = lambda: {
                    "accessToken": "t",
                    "expiresAt": (time.time() + (-100 if expired else 3600)) * 1000,
                }
                urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(error)
                return climits.probe("test", force=True)

            _, why, got = probe_with(http_error(403, "oauth_not_allowed_for_organization"),
                                     expired=True)
            check(got == "no_subscription" and "subscription" in why,
                  "403 with the organization code = closed subscription, not a stale login",
                  f"{got}: {why}")
            _, _, got = probe_with(http_error(401, None, "Invalid authentication credentials"),
                                   expired=True)
            check(got == "auth_expired", "401 with an expired token = stale login", got)
            _, _, got = probe_with(http_error(401, None, "Invalid authentication credentials"),
                                   expired=False)
            check(got == "denied", "401 with a live token = a plain refusal", got)
            _, _, got = probe_with(http_error(429, None, "rate limited"), expired=False)
            check(got == "throttled", "429 is a poll rate limit, not a loss of access", got)
            _, _, got = probe_with(urllib.error.URLError("no route"), expired=False)
            check(got == "unreachable", "a network failure is not passed off as a refusal", got)
        finally:
            climits.oauth_credentials, urllib.request.urlopen = creds_real, urlopen_real

        snap_any = snapshot(now, 10.0, 3600)
        cell = climits.access_state("auth_expired", snap_any, 35000)
        check(cell["state"] == "auth_expired" and "LOGIN EXPIRED" in cell["label"]
              and "9h" in cell["label"] and cell["hint"],
              "the table label names both the cause and the age of the data",
              json.dumps(cell))
        check(climits.access_state("fresh", snap_any, 10)["state"] == "live",
              "a confirmed poll means state `live`")
        check(climits.access_state("unreachable", None, None)["state"] == "no_data",
              "with no snapshot the state is `no data`, not the refusal cause")

        print("21. An overrun is visible in TIME, not only in percent")
        write_history(climits, [])          # layer 2 is irrelevant here — the line is clean
        debt = climits.evaluate(snapshot(now, 90.0, 100000),
                                budget(climits, dict(base, share=1.0)), "test", False)
        debt_row = debt["windows"][0]
        check(debt_row["verdict"] == "exceed" and debt_row["slack_seconds"] < 0,
              "the debt is computed in time and is negative",
              f"verdict {debt_row['verdict']}, slack {debt_row.get('slack_seconds')}")
        cell = climits.slack_cell(debt_row)
        check(cell.startswith("-") and any(unit in cell for unit in ("h", "m", "d")),
              f"slack prints with a sign and in units of time: {cell}")

        reports = [{"label": "test@example.com", "state_label": "live",
                    "verdict": debt["verdict"], "windows": debt["windows"],
                    "problems": debt["problems"]}]
        headers, table_rows, aligns = climits.summary_table(reports)
        check("slack" in headers and len(aligns) == len(headers),
              "the --all summary has a `slack` column",
              json.dumps(headers, ensure_ascii=False))
        slack_col = headers.index("slack")
        check(any(r[slack_col] == cell for r in table_rows),
              "the debt in time reached the summary row",
              json.dumps(table_rows, ensure_ascii=False))
        check(any(r[0] == "!!" for r in table_rows),
              "a window over the line is marked",
              json.dumps(table_rows, ensure_ascii=False))

        line = climits._problem_line(debt_row, with_margin=False)
        check(cell in line and "over the line" in line,
              f"the summary line names the overrun in time: {line}")

        # A debt larger than the window has left to live: the line will not recover it,
        # only the reset will.
        deep_row = climits.evaluate(snapshot(now, 99.0, 600),
                                    budget(climits, dict(base, share=1.0)),
                                    "test", False)["windows"][0]
        check("only the window reset" in climits._problem_line(deep_row),
              "an unrecoverable debt is labelled as such",
              climits._problem_line(deep_row))

        print("22. Everything below the cap is caught up WITHIN the window")
        # Regression: the line used to be `pace*elapsed% + burst` and therefore ended at
        # pace*100%. At pace = 0.9 a spend of 92% (below the 95% cap) was declared
        # unrecoverable. Now the slope grows linearly (0.9 nominal at the start -> 1.1 at
        # the end), the line reaches 100% at the reset, and only spend ABOVE the cap is
        # unreachable.
        write_history(climits, [])
        tail_cfg = budget(climits, dict(base, share=1.0))
        for used, left_share in ((92.0, 0.075), (94.9, 0.02), (60.0, 0.5)):
            left = int(TOTAL * left_share)
            row = climits.evaluate(snapshot(now, used, left), tail_cfg,
                                   "test", False)["windows"][0]
            check(row["catch_up_possible"] is True
                  and (row["wait_seconds"] or 0) < left,
                  f"spend {used}% is below the cap {row['cap_own']:.1f}% — caught up "
                  f"inside the window (in {climits.fmt_span(row['wait_seconds'])} with "
                  f"{climits.humanize(left)} left)",
                  f"reachable={row['catch_up_possible']}, wait {row['wait_seconds']} "
                  f"out of {left}s")
        # End of the window: the line arrives exactly at 100%, i.e. it runs into the cap
        # rather than into a cliff of its own.
        tail = climits._line_pp(1.0, 0.0, 0.9)
        check(abs(tail - 100.0) < 1e-9,
              f"the line reaches 100% at the reset for any pace (got {tail:.4f})")
        check(abs(climits._line_pp(0.5, 0.0, 1.0) - 50.0) < 1e-9,
              "pace 100% is the plain straight line, behaviour unchanged")
        # The inverse must actually be an inverse, or slack and catch-up time diverge.
        for pace in (1.0, 0.9, 0.7):
            for frac in (0.05, 0.5, 0.93):
                pp = climits._line_pp(frac, 1.19, pace)
                back = climits._line_seconds_at(pp, 1.19, pace, TOTAL) / TOTAL
                check(abs(back - frac) < 1e-6,
                      f"the line inverts exactly (pace {pace}, fraction {frac})",
                      f"got back {back:.6f}")
        # Spend above the cap is the one remaining unreachable case, and it is named.
        over_cap = climits.evaluate(snapshot(now, 97.0, TOTAL // 10), tail_cfg,
                                    "test", False)["windows"][0]
        check(over_cap["catch_up_possible"] is False
              and climits.slack_cell(over_cap) == "never"
              and "above the cap" in (climits._slack_note(over_cap) or ""),
              "spend above the cap is labelled `never` and the reason is named",
              f"{climits.slack_cell(over_cap)} / {climits._slack_note(over_cap)}")

        # A stale snapshot: the window reset long ago, there is no slack to report.
        expired = dict(debt_row, seconds_left=-500)
        check(climits.slack_cell(expired) == "—",
              "a reset window is not given invented slack", climits.slack_cell(expired))

        print("23. A long overrun is throttled, not turned into a hanging question")
        # The regression this guards: a 10-minute overrun in a night session put up a
        # permission dialog, which nobody answered until morning. The line caught up
        # ten minutes later, but a dialog is the CLI's — no hook can withdraw it, so
        # the session stayed frozen for hours over a problem that had already gone.
        write_history(climits, [])
        thr_cfg = budget(climits, dict(base, enforce=True,
                                       pace_factor={"interactive": 1.0, "headless": 1.0},
                                       max_wait_seconds={"interactive": 60, "headless": 300},
                                       throttle_max_seconds=7200), {"enabled": False})
        mild = climits.evaluate(snapshot(now, 51.6, half), thr_cfg, "test", False)
        mild_row = next(r for r in mild["windows"] if r["window"] == WINDOW)
        check(mild["verdict"] == "exceed" and 60 < mild_row["wait_seconds"] <= 7200,
              "the fixture is an overrun longer than the pause but shorter than the cap",
              f"wait {mild_row['wait_seconds']}s")

        here = climits._decide(mild, thr_cfg, False, 0, "test", False,
                               event="PreToolUse", attended=True)
        check(here["action"] == "throttle" and here["wait_seconds"] == 60,
              "with somebody at the keyboard the call is throttled, not asked about",
              f"{here['action']} / {here.get('wait_seconds')}")
        away = climits._decide(mild, thr_cfg, False, 0, "test", False,
                               event="PreToolUse", attended=False)
        check(away["action"] == "throttle" and away["wait_seconds"] == 300,
              "with nobody there the pause is the longer, headless one",
              f"{away['action']} / {away.get('wait_seconds')}")

        # Above the cap the line will never catch up, so throttling would be a lie:
        # that is the one case still worth a question — and a refusal when there is
        # nobody to ask.
        deep = climits.evaluate(snapshot(now, 99.0, half), thr_cfg, "test", False)
        refused = climits._decide(deep, thr_cfg, False, 0, "test", False,
                                  event="PreToolUse", attended=False)
        check(refused["action"] == "deny"
              and "ScheduleWakeup" in json.dumps(refused["output"]),
              "an unattended session is declined and told to reschedule, never asked",
              refused["action"])
        # The cooldown from scenario 11 is still ticking in the shared state dir —
        # retire it, or this would measure throttling of questions instead.
        climits._atomic_write(os.path.join(os.environ["CLIMITS_STATE_DIR"],
                                           f"{climits.account_key('test')}.ask.json"),
                              {"at": 0})
        asked = climits._decide(deep, thr_cfg, False, 0, "test", True,
                                event="PreToolUse", attended=True)
        check(asked["action"] == "ask",
              "the unrecoverable case is still a question when somebody is there",
              asked["action"])

        print("24. Who is at the keyboard: the clock, the switch, and the gate's guess")
        def at_local(hour: int, minute: int = 0) -> float:
            local = list(time.localtime(now))
            local[3], local[4], local[5] = hour, minute, 0
            return time.mktime(time.struct_time(tuple(local)))

        check(climits.in_unattended_hours("23:00-08:00", at_local(2))
              and climits.in_unattended_hours("23:00-08:00", at_local(23, 30))
              and not climits.in_unattended_hours("23:00-08:00", at_local(12)),
              "a night window crossing midnight is read as two sides, not one interval")
        check(not climits.in_unattended_hours(None, at_local(2))
              and not climits.in_unattended_hours("nonsense", at_local(2)),
              "an absent or malformed window disables the rule instead of failing")

        night_cfg = budget(climits, dict(base, unattended_hours="23:00-08:00"))
        climits.presence_write("test", None, None, "manual")
        check(climits.attendance("test", night_cfg, False, at_local(2))["source"] == "hours"
              and not climits.attendance("test", night_cfg, False, at_local(2))["attended"],
              "inside the night window the gate expects nobody")
        check(climits.attendance("test", night_cfg, False, at_local(12))["attended"],
              "outside it the session is treated as attended again")

        climits.presence_write("test", "here", now + 3600, "manual", "I am awake")
        state = climits.attendance("test", night_cfg, False, at_local(2))
        check(state["attended"] and state["source"] == "manual",
              "a hand-set `here` overrides the clock", json.dumps(state))
        # A claim is what somebody said; a measurement is what happened. Saying "I am
        # here" and then sleeping through a question is precisely where the claim is
        # the stale half, so the gate's own conclusion wins.
        climits._atomic_write(os.path.join(os.environ["CLIMITS_STATE_DIR"],
                                           f"{climits.account_key('test')}.ask.json"),
                              {"at": now - 3600, "pending": True})
        climits._ask_answered("test", night_cfg, True)
        check(not climits.attendance("test", night_cfg, False, at_local(12))["attended"],
              "a question slept through overrules the `here` that was claimed before it")
        climits.presence_write("test", "away", now + 3600, "manual")
        check(not climits.attendance("test", night_cfg, False, at_local(12))["attended"],
              "and a hand-set `away` overrides it the other way too")
        climits.presence_write("test", "away", now - 1, "manual")
        check(climits.attendance("test", night_cfg, False, at_local(12))["attended"],
              "an expired switch is no switch: a forgotten one must not last forever")

        # The gate's own guess. No hook fires while a dialog is up, so the gap since
        # the question was issued IS how long it hung unanswered.
        climits.presence_write("test", None, None, "manual")
        ask_file = os.path.join(os.environ["CLIMITS_STATE_DIR"],
                                f"{climits.account_key('test')}.ask.json")
        climits._atomic_write(ask_file, {"at": now - 3600, "pending": True})
        guess = climits._ask_answered("test", night_cfg, True)
        check(guess and guess["mode"] == "away" and guess["source"] == "auto",
              "a question that hung an hour makes the gate call itself unattended",
              json.dumps(guess))
        check(not climits.attendance("test", night_cfg, False, at_local(12))["attended"],
              "and that verdict holds through the day, not only at night")
        check(climits._ask_answered("test", night_cfg, True) is None,
              "the same question is not measured twice")
        climits.presence_write("test", None, None, "auto")

        # Falling asleep cannot be measured until a question has already frozen the
        # session for the night, so an evening `here` must not reach the night at all.
        opens = climits.next_unattended_start("23:00-08:00", at_local(21))
        check(opens is not None and time.localtime(opens).tm_hour == 23
              and opens - at_local(21) == 2 * 3600,
              "the next opening of the night window is found on the clock",
              str(opens))
        check(climits.next_unattended_start(None) is None,
              "with no night window there is nothing to cap a `here` against")
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        shutil.rmtree(state, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failures out of {CHECKS} checks:")
        for title in FAILURES:
            print(f"  - {title}")
        return 1
    print(f"all {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
