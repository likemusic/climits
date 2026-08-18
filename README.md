# climits

**A pace governor for Claude Code subscription limits.**

Claude Code tells you that you are out of quota at the moment you run out. climits
tells your *session* to slow down before that — and, if you let it, makes the
session wait, ask, or decline on its own.

```
account        you@example.com
state          live
snapshot       api (fresh)
plan           max (default_claude_max_5x)
mode           interactive

  window      used  allowed      free    slack  elapsed   reset
  five_hour  21.0%    57.3%  +36.3 pp  +1h 49m    47.3%  2h 38m
  seven_day   3.0%    33.0%  +30.0 pp   +2d 2h    23.4%   5d 8h

verdict:  within the pace line
```

## What problem this solves

There are plenty of good usage *monitors* for Claude Code — status lines, tray
apps, dashboards. They answer "how much is left". There are also *auto-resumers*,
which watch the terminal for "usage limit reached" and type `continue` once the
window rolls over. Both react to the wall: one draws it coming, the other cleans up
after you hit it.

climits is the other half. It converts "how much is left" into "how much you are
allowed to have spent **by this minute**", and it hangs on hooks, where it can act:

| verdict | what happens |
|---|---|
| `ok` | nothing at all |
| `soft` | one muted warning line: you are approaching the line |
| `exceed`, short | the hook **sleeps** until the line catches up, then lets the call through — the session never notices |
| `exceed`, long, interactive | you are **asked** once (not per tool call): continue anyway or wait |
| `exceed`, long, headless | the call is **declined** with a growing backoff, and the agent is told to call `ScheduleWakeup` and end the turn — the session is never killed |

That last row is the reason this exists: an overnight autonomous run that hits a
limit at 3am should pause and resume, not die.

## The pace line

For each window (5-hour, weekly, per-model weekly) climits knows the reset time,
which gives it the fraction of the window already elapsed. The line is:

    allowed(f) = burst + 100 * (k*f + (1-k)*f²)

where `f` is the elapsed fraction, `burst` is a head start (otherwise the first
message of a window hits a wall at 0% allowed), and `k` = `pace_factor` bends the
line: `k = 1` is a straight "as much of the window as has passed, that much quota",
`k < 1` holds back early and catches up later. Either way the line reaches 100% at
the reset, so the tail of a window is never wasted.

`burst` and the warning threshold are both configured **in minutes**, not points,
because a percentage point is not comparable across windows: the weekly line is 33x
flatter than the five-hour one, so "15 pp of slack" is 46 minutes on one and more
than a day on the other. Everything the tool reports about margins it reports in
time for the same reason.

Details and the reasoning behind the shape: [docs/design.md](docs/design.md).

## Install

As a plugin (this installs the hooks for you):

```
/plugin marketplace add likemusic/climits
/plugin install climits@climits
```

Or clone and run it standalone — it is a single Python file with no dependencies:

```
git clone https://github.com/likemusic/climits
./climits/bin/climits status
```

To wire the hooks by hand instead of installing the plugin, add this to
`~/.claude/settings.json` (`timeout` must exceed your largest `max_wait_seconds`,
or the hook is killed mid-pause):

```json
"hooks": {
  "UserPromptSubmit": [
    {"hooks": [{"type": "command", "command": "/path/to/climits/bin/climits gate", "timeout": 330}]}
  ],
  "PreToolUse": [
    {"matcher": "*",
     "hooks": [{"type": "command", "command": "/path/to/climits/bin/climits gate", "timeout": 330}]}
  ],
  "SubagentStop": [
    {"hooks": [{"type": "command", "command": "/path/to/climits/bin/climits gate", "timeout": 330}]}
  ]
}
```

Optionally, as a status line — this costs nothing, since Claude Code hands the
status line a `rate_limits` block on stdin with no network call involved:

```json
"statusLine": {"type": "command", "command": "/path/to/climits/bin/climits feed"}
```

**Nothing is enforced until you say so.** Out of the box the gate only observes and
writes its decisions to a log. Turn it on with `"enforce": true` in the config, or
per invocation with `climits gate --enforce`.

## What it reads, what it sends

Worth being explicit about, since this thing runs on every tool call:

- **Reads** `~/.claude/.credentials.json` — the OAuth token the Claude Code CLI
  already stores there — to authenticate its own quota request.
- **Calls** `GET https://api.anthropic.com/api/oauth/usage` at most once per
  `poll_ttl_seconds` (300 by default), shared across every session on the machine.
  This is the same undocumented endpoint the CLI uses for `/usage`.
- **Runs** `claude auth status --json` to learn which account is active (cached
  until the credentials file changes).
- **Reads** the tail (64 KB) of the current session transcript to learn which model
  is answering, and — on `SubagentStop` only — the finished subagent's transcript to
  count its tokens.
- **Writes** only to `~/.claude/state/limits/`: the snapshot, a history log, a gate
  log, and cooldown stamps.

It sends nothing anywhere else. No telemetry, no third-party service, no
dependencies outside the Python standard library.

## Commands

From inside a session, `/climits:status` runs the report and has Claude read it back
to you in a sentence or two — where you stand relative to the line, which window is
binding, and what it would take to get back on pace. It passes its arguments
through, so `/climits:status --all` and `/climits:status agents` work too.

Installing the plugin also puts `climits` on the Bash tool's PATH, and the commands
below work from any shell if you add the repository's `bin/` to your own PATH.

| command | what it does |
|---|---|
| `climits status` | the table above; `--json` for machines |
| `climits status --all` | every account on the machine, including `~/.claude/profiles/*` |
| `climits probe --force` | refresh the snapshot now, ignoring the TTL |
| `climits agents [--hours N]` | exact token spend of finished subagents, by agent type |
| `climits feed` | status line bridge (reads the payload on stdin) |
| `climits gate [--enforce]` | the hook entry point |
| `climits speed` | output of the shared-account experiment, off unless enabled |

## Configuration

`~/.claude/limits-budget.json` (see [config.example.json](config.example.json)).
Everything has a default; the file is optional.

| key | meaning |
|---|---|
| `enforce` | `false` (default) — observe and log only; `true` — act on the verdict |
| `burst_minutes` | head start, per window, in minutes of line growth |
| `soft_margin_minutes` | how far before the line the `soft` warning starts |
| `reserve_percent` | top of the quota never spent, so a task in flight can finish |
| `max_wait_seconds` | longest silent pause; beyond it you get asked (or declined) |
| `ask_cooldown_minutes` | how rarely the question may be asked |
| `warn_cooldown_minutes` | how rarely the warning line may be printed |
| `scoped_windows` | `auto` (default) / `always` / `never` — see below |
| `pace_factor` | bends the line: `< 1` spends slower early, catches up late |
| `poll_ttl_seconds` | how often the API may be polled, machine-wide |

Values that differ by mode take an object: `{"interactive": 60, "headless": 300}`.
Values that differ by window take an object keyed by window name:
`{"five_hour": 30, "seven_day": 960}`. Per-account overrides go under
`accounts["you@example.com"]`.

### Per-model weekly windows

A weekly limit scoped to one model constrains only that model: with one model's
weekly quota exhausted you can keep working on another. So by default such a window
gates only the session that actually answers with that model (`scoped_windows:
auto`); elsewhere it shows up as an informational `~` row and does not stop
anything. `always` restores blunt behaviour, `never` ignores those windows
entirely.

### Shared accounts

On an account several people log into, the counter is common: your percentages
include everybody's spend. The pace line still does the useful half of the job —
it paces *the counter*, which is the shared resource, and a counter that is spent
evenly survives to the end of the week. No attribution is needed for that, and
none is attempted.

There is also an experiment in the tree, `lib/foreign.py`, which tries to estimate
how fast *other people* are spending and to reserve their share. **Do not treat it
as a feature.** What it can actually observe is the counter growing while this
machine was idle; turning that into a per-person rate is an extrapolation, biased
upward by construction, and it cannot tell one colleague from another or from your
own second machine. It is disabled by default, it is not loaded unless you switch
it on, and deleting the file changes nothing else.

If you want to know exactly what it does and does not do before touching it, that
is written down in [docs/shared-account.md](docs/shared-account.md) — including the
list of what remains unfinished.

## Development

```
python3 tests/selftest.py
```

94 checks, no network and no live account: the state directory is replaced with a
temporary one, the account comes from an environment variable, and snapshots,
history and transcripts are built by hand. Anything that changes the line, the gate
policy, the access states or the shared-account layer should show up here.

Releasing, installing across profiles, and the traps that `claude plugin validate` does not catch: [docs/maintenance.md](docs/maintenance.md).

## Requirements

Python 3.9+ (standard library only), the `claude` CLI on `PATH`, and a Claude
subscription — the quota endpoint is not available for API-key usage. Developed on
Linux/WSL; the paths are POSIX-style, `fcntl` locking is used, so Windows needs
WSL.

## Status

Early. The core has been running against real accounts for weeks, but this is its
first packaged release: expect rough edges in the parts that only appear on other
people's setups (unusual plans, other profile layouts, macOS paths). Issues and
pull requests welcome.

## License

MIT © Valery Ivashchanka
