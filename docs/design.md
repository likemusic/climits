# Design notes

Why the tool is shaped the way it is. Reading this is not required to use it.

## The unit problem: points are not comparable, time is

The API reports each window as a percentage. That number is useless for comparing
windows against each other. The five-hour line grows at 20 pp/h, the weekly line at
0.595 pp/h — a factor of 33. "15 points of headroom" means 46 minutes on one window
and more than a day on the other.

So every margin in climits is stated in **time**:

- `burst_minutes` — the head start, as "how much the line grows in N minutes";
- `soft_margin_minutes` — how far ahead of the line the warning begins;
- the `slack` column — `+2h 10m` (the line is that far behind you) or `-40m` (it
  needs that long to catch up);
- the tightest window is chosen by slack in seconds, not by points. Chosen by
  points, the wrong window regularly came out as the binding one.

Thresholds are converted from time into points at the **nominal** rate of the
window (100% per window), never at the momentary slope of the line — that slope
varies within a window, and a threshold based on it would change several-fold
between the start and the tail.

## The line

    allowed(f) = burst + 100 * (k*f + (1-k)*f²)

`f` — the fraction of the window elapsed, from the reset time the API reports.
`burst` — a head start, without which the first minutes of a window allow ~0% and
the first message hits a wall; it also legalises a burst of work after an idle
stretch. `k` = `pace_factor` ∈ (0, 1].

The quadratic term is what makes the slope grow: from `k`×nominal at the start of
the window to `(2−k)`×nominal at the end. At `k = 1` the line is the plain straight
"as much of the window as has passed, that much quota is allowed".

This replaced an earlier form, `k*elapsed% + burst`, which had two flaws:

1. It ended at `k*100%`. The band between that and the cap was unreachable
   **forever** — at `k = 0.9` the line stopped at 91.2%, so a spend of 92% (still
   below the 95% cap) was reported as "the line will never catch up", which was
   simply false.
2. It spread the holding-back evenly over the window. The place for restraint is
   the *beginning*: unspent quota burns at the reset anyway, and spend made right
   before a reset is cleared almost immediately.

`k > 1` has no shape — the line would run above 100% and hit the cap regardless —
so it is clamped to 1.

The inverse (`_line_seconds_at`) is solved exactly, as the root of the quadratic,
rather than by dividing by "the speed of the line": that speed is not constant.
Below `burst` the line is continued backwards at the nominal slope, otherwise the
head start would collapse to zero in the first minutes of a window for no reason.

## Cap, not slope

Two things bound the allowance: the line and a **cap** (`100% − reserve_percent`,
minus whatever layer 2 reserves for other people). They are different in kind.

The reserve is taken off the *cap*, never off the slope. Trimming the slope would
apply the concession to the whole elapsed window — so somebody else appearing now
would retroactively take away quota you already spent. The cap only touches what
has not been spent yet.

This also gives a clean answer to "can waiting help?": everything below the cap is
recovered by the line within the window (it reaches 100% at the reset), so the only
unrecoverable case is a spend **above the cap**. That is what `slack: never` means,
and it is stated in words rather than as a number, because the number would
saturate at the reset time and read as "catches up exactly at the reset".

## Verdicts and what the gate does with them

```
exceed   used >= allowed
soft     used >= allowed - margin
ok       otherwise
```

`unknown` (no data at all) never blocks anything. That is a rule, not an accident:
a governor that fails closed on a network glitch is worse than no governor.

The gate's response to `exceed` depends on how long the wait would be, not on how
bad the number looks:

- shorter than `max_wait_seconds` → sleep, then allow. The session does not notice,
  and both interactive and autonomous runs continue by themselves. The sleep covers
  **all** windows over the line, not just the tightest — otherwise the pause ends
  while the second window is still behind and the next call trips again.
- longer, interactive → ask, at most once per `ask_cooldown_minutes`.
- longer, headless → decline, with a backoff that doubles per consecutive strike,
  and tell the agent to schedule a wake-up and end its turn. **The session is never
  ended.** An overnight run must survive days of refusals and continue when the line
  grows.

Two implementation details that are easy to get wrong:

- `permissionDecision` is only understood by `PreToolUse`. On other events the
  right to ask is deliberately *not* spent — otherwise `UserPromptSubmit`, which
  runs first on every prompt, would consume the cooldown and return an answer the
  CLI ignores, so the question would never reach a human.
- The hook `timeout` in settings must exceed `max_wait_seconds`, or the pause is
  killed halfway through.

## Per-model weekly windows

A weekly window scoped to one model constrains only that model. Gating every
session on it means a session working on a different model is stopped by a quota it
does not spend — a false alarm that trains people to ignore the marker.

So a scoped window gates only when its model is the one answering. The model is
learned from the tail of the session transcript (the hook payload does not carry
it), cached for a couple of minutes because the gate runs on every tool call. A
subagent may run a different model than the main loop, and it really does spend
that model's window — so on `SubagentStop` its models are folded into the session's.

## Subagent accounting

`SubagentStop` is the only event that carries a *fact* of spend: its payload has the
path to the finished agent's transcript. One model message appears in that file as
several records (streaming snapshots plus the final one) sharing a `message.id`, so
a line-by-line sum inflates the total roughly threefold; climits takes one record
per id, the one with the larger `output_tokens`.

Cache reads are counted separately and excluded from the headline number: they are
an order of magnitude cheaper, and mixing them in is a reliable way to inflate the
figure.

Also note that a tool call made *inside* a subagent carries the same `session_id`
as the main loop. Only `agent_id` / `agent_type` distinguish them.

## State

Everything lives in `~/.claude/state/limits/`, keyed by a hash of the account
address — **one directory per machine**, not per config dir. Profiles
(`~/.claude/profiles/*`) have their own config dirs, and splitting the state along
with them would make layer 2 read your own activity in a neighbouring profile as
somebody else's.

The API poll is guarded by a non-blocking `flock`: the snapshot is shared, so a
session that finds the lock taken simply uses what is there.
