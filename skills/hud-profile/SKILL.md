---
name: hud-profile
description: >-
  Render replies for a low-resolution heads-up display (Even Realities G2 glasses).
  Use when supervising a session from the glasses / on a hike, or whenever told to
  render for the HUD. hike-mode prepends a "you're on a hike — use the hud-profile
  skill" preamble to the first turn of each resumed glasses session, so this engages
  without being asked; you can also invoke it on the laptop to preview the output.
---

# HUD profile — rendering for the glasses

You're being read on a small heads-up display, glanced at while moving (walking,
hiking, hands busy). Optimize every reply for *scannability at a glance*, not for
completeness. Treat roughly **7 rows of ~9 words** as one screen.

## Lead with the answer

- First line = the verdict / answer / result. No preamble, no "Sure, I can…".
- Then at most a few short supporting lines.
- Last line offers the next step: `more?`, `expand?`, or a tap-sized question.

## One screen at a time (progressive disclosure)

- Keep the first reply to ~one screen. If there's more, say so and wait — don't
  dump it. On `more?`/`expand?`, send the next chunk, still one-thought-per-line.
- **Never drop a caveat to save a row — move it to the expansion.** Accuracy first;
  brevity is about *ordering and chunking*, not omission. (Credit: memo-flow's
  `pager` skill, the source of this rule and much of this profile.)

## Formatting for a tiny screen

- One thought per line. Prefer line breaks over commas.
- No wide tables — use `key: value` pairs instead.
- No ASCII art, and no boxes/rules wider than the screen.
- Code: only if it's under ~5 lines. Longer code → name the file path for the
  laptop, don't paste it.
- Never truncate file paths or URLs — they must stay exact and copy-pasteable,
  even if a path eats a whole row.

## Questions, approvals, and handing back — use `AskUserQuestion`

`AskUserQuestion` is how you reach the user on the glasses: even-terminal turns it
into a ring-tappable card *and* it pings them (same "awaiting" state a permission
prompt uses). A plain text reply that just ends the turn does NOT notify them — it
goes unnoticed until they happen to look. So:

- **When you finish a unit of work or otherwise hand control back, close the turn
  with an `AskUserQuestion`** — that's the poke. Deliberately: once when you genuinely
  hand back, not after every intermediate step.
- **Make the question stand on its own.** When the card pops up it's ALL the user
  sees — they can't scroll back to your message behind it, and the card itself doesn't
  scroll. So the `question` field must lead with a one-line recap of where things are,
  then the ask. Never say "see above" or assume they read your prior text. Keep it to
  ~2–3 short lines.
- **Favor the free-text answer.** Most hand-backs are open-ended, so phrase the
  question so *speaking a free reply is the natural response*. The tap options are
  only shortcuts for the 1–2 most likely next steps; the user can always pick the
  free-text ("Other") option and just talk. Don't cram an open-ended moment into rigid
  boxes — offer a couple of shortcuts and invite them to say the rest.
- For approvals (a commit, a shell command, a risky edit), describe what it will
  *do* in plain language — the effect, not the raw command (gnarly one-liners and
  cryptic flags are noise on the HUD). A commit message is readable, so show it; for a
  shell command or diff, summarize the effect (what changes, what's touched). Then ask
  via `AskUserQuestion` (Run / Cancel). Don't act first and report after.

Example hand-back:

```
question: "Auth refactor done — 12/12 tests pass, not committed yet.
           Commit it, move to the API, or tell me what next?"
options:  Commit · Start API      (or just say what you want)
```

## When in doubt

Shorter. If you're unsure whether to include something, push it to the expansion
and offer `more?`.
