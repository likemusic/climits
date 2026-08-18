---
description: Show subscription usage against the pace line (5h / weekly windows)
argument-hint: "[status | status --all | probe --force | agents | speed]"
allowed-tools: Bash(*/bin/climits:*)
---

Current climits report:

!`"${CLAUDE_PLUGIN_ROOT}/bin/climits" ${ARGUMENTS:-status}`

Read the table above and tell the user, in two or three sentences:

- whether they are ahead of or behind the pace line, and by how much **in time**
  (the `slack` column: `+2h 10m` means the line is that far behind them, `-40m`
  means the line needs that long to catch up);
- which window is the binding one right now and when it resets;
- only if a window is marked `!` or `!!`: what it would take to get back on pace —
  waiting the stated time, or waiting for the reset when the `slack` column says
  `never`.

Do not repeat the table. Do not speculate about limits the report does not show.
If the report says the quota cannot be read, relay the `what to do` line as is.
