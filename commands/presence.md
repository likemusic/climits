---
description: Tell climits whether anybody is at the keyboard (here / away / auto)
argument-hint: "[here|away|auto] [--for 8h | --until HH:MM]"
allowed-tools: Bash(climits:*)
---

Run `climits presence $ARGUMENTS` with the Bash tool — plain `climits presence` when
no arguments were given, which reports the current state and changes nothing.

This setting decides one thing: whether the gate may put up a question when the
spend runs over the pace line. A question can only be answered by a person, and an
unanswered permission dialog freezes the session until somebody presses a key — the
dialog belongs to the CLI and no hook can withdraw it, so it outlives the overrun it
was asked about. With nobody there the gate throttles instead, and declines with a
scheduled wake-up when throttling would no longer help.

Report back in one or two sentences: what the state is now, and what it changes
about the gate's behaviour. Mention when it expires — an override is never
open-ended, because a `here` forgotten in the evening would switch off the night
policy for good.

Take the user's own words as the intent: "I am going away", "I am back", "I will be
at the keyboard for another hour" map to `away`, `here`, and `here --for 1h`. Use
`auto` to hand the decision back to the night window and the gate's own guess.
