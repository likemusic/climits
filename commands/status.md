---
description: Show subscription usage against the pace line (5h / weekly windows)
argument-hint: "[--all | probe --force | agents | speed | presence]"
allowed-tools: Bash(climits:*)
---

Run `climits $ARGUMENTS` with the Bash tool — plain `climits status` when no
arguments were given. The plugin puts `climits` on the Bash tool's PATH, so no path
is needed.

Then answer in two parts, in this order.

**First, the report itself.** Paste what the command printed into a fenced code
block, verbatim and complete — the head rows and the table. The terminal collapses
long tool output to a couple of lines, so a report that is only "on screen" is not
actually readable; without this the figures below have nothing behind them. Do not
reformat the columns, do not drop rows, do not round anything.

**Then, below it, two or three sentences of reading:**

- whether they are under or over the pace line, and by how much **in time**. The
  `slack` column carries this. `+2h 10m` means the line passed their current spend
  2h 10m ago — they are under-spent by that much of the window's travel, which is
  their cushion. `-40m` means they are over the line and it needs 40 minutes to
  catch up with what they have already spent;
- which window is the binding one right now, and when it resets;
- only when a window is marked `!` or `!!`: what getting back on pace would take —
  waiting the stated time, or waiting for the reset when the `slack` column reads
  `never` (which means the spend is above the window cap and the line alone will not
  recover it).

Do not convert the figures into predictions the report does not make. If the report
says the quota cannot be read, relay its `what to do` line as it stands rather than
guessing at the cause.
