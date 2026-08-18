# Layer 2: shared accounts

**Status: an experiment, disabled by default, unfinished — not a feature.** It runs,
and it has been exercised against a real shared account, but the thing it reports is
an extrapolation of something only indirectly observable. It is documented here
rather than advertised in the README on purpose. Read all of this before turning it
on, and treat the number it produces as a hypothesis, not a measurement of anybody's
usage.

## The problem

On a personal subscription the counter is yours, and the pace line in the core is
the whole story.

On an account several people log into, the counter is **shared**. Your `status`
shows the sum of everybody's spend; the API offers no per-person breakdown; and any
threshold on that counter stops everyone at once, not just whoever crossed it. The
core's line still paces *the counter*, but it cannot tell whether you are the one
filling it.

## What was rejected

**A fixed per-person share** (`share: 0.25` for a team of four) was the obvious
first idea and it does not work. The counter is common and your own contribution is
invisible in it, so a share is a fiction: if your colleagues are idle you throttle
yourself for nothing, and if they are busy the counter passes your threshold on
their spend alone and stops you anyway. A number that is wrong in both directions
is not a policy.

**A static `pace_factor`** (spend at 70% of nominal to leave room) is the same
fiction with fewer digits. It was in the config for a while, entirely made up. It
survives only as the fallback for when there is no measurement.

## What it does instead

Measure, do not assume.

The gate runs **before** each of your own actions, which means climits knows the
intervals in which you made no move at all. The growth of the counter across such
an interval is other people's spend, in the plainest possible sense. Their rate,
extrapolated to the reset, is then **reserved for them by trimming your cap** — not
by bending your line (see [design.md](design.md), "Cap, not slope").

`climits speed` shows the measurement and what it implies:

```
  window     nominal   others         ours     reserve   cap     allowed
  five_hour  20.00/h   1.75/h (9%)    18.25/h  2.1 pp    92.9%   57.4%
```

## Deliberate biases and hard limits

- **Quantisation.** The counter moves in whole percent. One point of a weekly
  window is 100.8 minutes of nominal pace; of a five-hour window, 3 minutes. Short
  intervals are therefore noise, and the module requires both a minimum interval
  (`min_gap_seconds`) and a minimum total (`min_total_seconds`) before it reports
  anything at all.
- **Biased upward, on purpose.** Your own last move before an idle stretch lands at
  the start of the interval — the gate fires before the work, while the spend
  appears in the counter after it — and is therefore counted as somebody else's.
  The direction was chosen deliberately: overstating other people's load slows *you*
  down, whereas understating it would eat into their share. `slack_pp` damps what
  remains.
- **A rate, not an identity.** Two colleagues at half speed and one at full speed
  are the same number here. There is no way to attribute spend to a person.
- **Extrapolation is capped.** A short measurement projected over days could zero
  your quota entirely, so the reserve never takes more than `reserve_cap_share` of
  what is left.
- **Autonomous runs cede more.** `reserve_factor` is 1.0 interactive and 1.5
  headless: an unattended run should give colleagues a wider berth than a person
  sitting at the keyboard.

## What is not done

The honest list, in the order it would matter:

1. **Retrospective reconciliation.** Nothing checks the estimate against reality
   after the fact — comparing predicted foreign spend with the actual counter growth
   over a completed window would show whether the bias is 10% or 300%. The history
   file has everything needed for it.
2. **Idle detection is machine-wide, not person-wide.** "We made no move" means no
   move *on this machine*. Two machines on the same account each see the other as a
   stranger, which is right, but neither knows it is being seen that way.
3. **No feedback into the reserve.** The reserve is a straight extrapolation of a
   measured rate; it does not learn, decay, or react to a colleague going quiet
   other than through the lookback window.
4. **Parameters are hand-tuned.** `min_gap_seconds`, `lookback_seconds`,
   `slack_pp` and friends were set by hand against one account's data. There is no
   evidence they transfer.

## Enabling it

```json
{"defaults": {"foreign": {"enabled": true}}}
```

or per account:

```json
{"accounts": {"team@example.com": {"foreign": {"enabled": true}}}}
```

Then watch `climits speed` for a day or two before trusting it. Until there are
enough clean intervals it reports "not measured" and the configured pace applies —
which is the correct behaviour, not a failure.

The history it feeds on (`~/.claude/state/limits/*.history.jsonl`) is written
whether or not layer 2 is enabled, so switching it on later works against data
already collected. Deleting `lib/foreign.py` disables it structurally.
