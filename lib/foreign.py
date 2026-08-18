"""Layer 2 — pace control on a SHARED account (optional add-on).

On a personal subscription the usage counter is yours alone, and the pace line in
the core is the whole story. On an account shared by several people the counter is
common: every colleague's spend shows up in your percentages, a threshold on the
common counter stops everybody at once, and there is no per-person breakdown in the
API. This module estimates the OTHER people's load and reserves their forecast for
them.

How it estimates. The gate runs BEFORE each of our own actions, so it knows the
intervals in which we made no move at all. The growth of the counter in such an
interval is other people's spend, pure. Their forecast until the reset is then
subtracted from OUR CAP — not from the slope of the pace line. Trimming the slope
would be wrong: the slope applies to the whole elapsed window, so colleagues
appearing now would retroactively take away quota we already spent. The cap only
touches the part that has not been spent yet.

Known limits of the estimate, all deliberate:

  * The counter moves in whole percent, so short intervals are quantisation noise:
    1 pp of a weekly window is 100.8 minutes of nominal pace, of a five-hour one
    3 minutes. Hence the minimum interval length and the minimum total.
  * The estimate is biased UPWARD: our own last move before an idle stretch lands at
    the start of the interval (the gate fires before the work, the spend appears in
    the counter after it) and would otherwise be counted as somebody else's. The
    direction was chosen on purpose — overstating other people's load slows US down
    rather than eating into their share. `slack_pp` damps what is left.
  * It is a rate, not an identity: two colleagues at half speed and one at full
    speed are the same number here.

This is the least settled part of climits, which is why it ships as a separate,
disabled-by-default module. Delete this file and the core keeps working; the
history file it feeds on is written regardless, so it can be switched on later
against data already collected.

Enable it in ~/.claude/limits-budget.json:

    {"defaults": {"foreign": {"enabled": true}}}
"""

from __future__ import annotations

import json
import time

DEFAULTS = {
    "enabled": False,
    # The counter moves in whole percent, so short intervals are quantisation noise.
    "min_gap_seconds": {"five_hour": 900, "seven_day": 7200},
    "lookback_seconds": {"five_hour": 18000, "seven_day": 172800},
    "min_total_seconds": {"five_hour": 1800, "seven_day": 10800},
    # Subtracted from the growth of every interval, damping the quantisation step
    # (1 pp) and the contribution of our own last move.
    "slack_pp": 0.5,
    # An autonomous run cedes more than manual work does: reserve their forecast with
    # a margin. This is where the asymmetry between modes lives.
    "reserve_factor": {"interactive": 1.0, "headless": 1.5},
    # Extrapolating a short measurement over days could zero our quota entirely, so
    # the reserve never eats more than this share of what is left.
    "reserve_cap_share": 0.75,
    # A measured load REPLACES the static pace_factor: that knob was a placeholder
    # with no physical meaning. With no data, the configured pace applies as before.
    "override_pace": True,
}


def _by_key(value, key: str, fallback: float) -> float:
    if isinstance(value, dict):
        return float(value.get(key, fallback))
    return float(value) if value is not None else fallback


def _params(params: dict | None) -> dict:
    return {**DEFAULTS, **(params or {})}


def measure(history: list[dict], window: str, params: dict,
            now: float | None = None) -> dict | None:
    """Rate of OTHER people's spend (pp/h) over intervals with no calls of ours.

    Returns None when there are too few clean intervals: the configured pace applies
    then."""
    params = _params(params)
    now = time.time() if now is None else now
    min_gap = _by_key(params.get("min_gap_seconds"), window, 7200.0)
    lookback = _by_key(params.get("lookback_seconds"), window, 172800.0)
    min_total = _by_key(params.get("min_total_seconds"), window, 10800.0)
    slack = float(params.get("slack_pp", 0.5))
    clean_seconds = 0.0
    delta_pp = 0.0
    intervals = 0
    since = None
    for prev, cur in zip(history, history[1:]):
        if window not in prev["w"] or window not in cur["w"]:
            continue
        gap = cur["t"] - prev["t"]
        if gap < min_gap or cur["t"] < now - lookback:
            continue
        if cur["act"] > prev["t"]:
            continue                    # we worked inside the interval — not a sample
        used_prev, reset_prev = prev["w"][window]
        used_cur, reset_cur = cur["w"][window]
        if reset_prev and reset_cur and reset_prev != reset_cur:
            continue                    # the window reset: values are incomparable
        if used_cur < used_prev:
            continue                    # same reset, but its moment is unknown
        clean_seconds += gap
        delta_pp += max(0.0, used_cur - used_prev - slack)
        intervals += 1
        if since is None:
            since = prev["t"]
    if not intervals or clean_seconds < min_total:
        return None
    return {"rate_pph": delta_pp / (clean_seconds / 3600.0), "delta_pp": delta_pp,
            "clean_seconds": int(clean_seconds), "intervals": intervals, "since": since}


def reserve(measurement: dict, params: dict, mode: str, ceiling: float,
            own: float, seconds_left: int) -> tuple[float, float]:
    """How much of the cap is reserved for the other people, and at what rate.

    Returns (reserve_pp, reserve_rate_pp_per_second). The rate is what the core uses
    to work out when the cap will have risen enough to cover an overrun."""
    params = _params(params)
    factor = _by_key(params.get("reserve_factor"), mode, 1.0)
    rate = measurement["rate_pph"] / 3600.0 * factor
    room = max(0.0, ceiling - own)
    return (min(rate * seconds_left, room * float(params.get("reserve_cap_share", 0.75))),
            rate)


def cmd_speed(argv: list[str], core) -> int:
    """`climits speed` — what exactly was measured and what follows from it.

    Material for working out why the gate is (or is not) holding things back at a
    given moment. `core` is the main climits module, passed in explicitly so the
    dependency runs one way only."""
    budget = core.load_budget()
    email = core.account_email()
    headless = core._is_headless()
    settings = core.settings_for(budget, email)
    foreign_cfg = _params(settings.get("foreign"))
    history = core.history_read(email)
    ttl = float(settings.get("model_seen_ttl_minutes", 60)) * 60
    report = core.evaluate(core.read_snapshot(email), budget, email, headless,
                           core.models_recent(email, ttl))
    rows = []
    for row in report["windows"]:
        window = row["window"]
        measurement = row.get("foreign_measure")
        norm = 100.0 / (core.WINDOW_SECONDS.get(window, core.DEFAULT_WINDOW_SECONDS)
                        / 3600.0)
        rows.append({
            "window": window, "norm_pph": norm,
            "foreign_pph": measurement["rate_pph"] if measurement else None,
            "ours_pph": None if not measurement else max(0.0, norm - measurement["rate_pph"]),
            "measure": measurement, "reserve_pp": row["foreign_reserve_pp"],
            "cap_own": row["cap_own"], "allowed_own": row["allowed_own"],
            "pace_source": row["pace_source"], "pace_factor": row["pace_factor"],
            "min_total_seconds": _by_key(foreign_cfg.get("min_total_seconds"), window,
                                         10800.0),
        })
    if "--json" in argv:
        print(json.dumps({"account": email,
                          "mode": "headless" if headless else "interactive",
                          "points": len(history), "windows": rows},
                         ensure_ascii=False, indent=2))
        return 0

    core.print_table(["", ""], [["account", email or "unknown"],
                                ["mode", "headless" if headless else "interactive"],
                                ["history points", str(len(history))]])
    print()
    if not foreign_cfg.get("enabled"):
        print("layer 2 is disabled in the config (foreign.enabled = false)")
        return 0

    headers = ["window", "nominal", "others", "ours", "reserve", "cap", "allowed"]
    table, notes = [], []
    for row in rows:
        measurement = row["measure"]
        if measurement:
            others = (f"{measurement['rate_pph']:.2f}/h "
                      f"({core.norm_share(measurement['rate_pph'], row['window']):.0f}%)")
            ours = f"{row['ours_pph']:.2f}/h"
            notes.append(f"{row['window']}: {measurement['intervals']} clean intervals "
                         f"over {core.humanize(measurement['clean_seconds'])}, growth "
                         f"{measurement['delta_pp']:.1f} pp")
        else:
            others, ours = "not measured", "—"
            notes.append(f"{row['window']}: too few clean intervals (need "
                         f"{core.humanize(int(row['min_total_seconds']))}) -> pace "
                         f"from config {row['pace_factor']:.0%}")
        table.append([row["window"], f"{row['norm_pph']:.2f}/h", others, ours,
                      f"{row['reserve_pp']:.1f} pp" if measurement else "—",
                      f"{row['cap_own']:.1f}%" if measurement else "—",
                      f"{row['allowed_own']:.1f}%"])
    core.print_table(headers, table, aligns="<>>>>>>", indent="  ")
    print()
    for note in notes:
        print(f"  {note}")
    return 0
