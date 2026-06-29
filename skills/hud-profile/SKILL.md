---
name: hud-profile
description: >-
  Render replies for a low-resolution heads-up display (Even Realities G2 glasses).
  Use when supervising a session from the glasses / on a hike, or whenever told to
  render for the HUD. hike-mode injects "use the hud-profile skill" as the first
  glasses turn; you can also invoke it on the laptop to preview how output will look.
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

## Questions and approvals are tap-sized

- **Ask with the `AskUserQuestion` tool, not prose options.** even-terminal renders
  it as a ring-tappable menu (its dedicated AskUserQuestion handler) — so the answer
  is a tap, not a spoken reply. Writing out "A) … B) …" in text forces the user to
  answer by voice, which is worse on the glasses. One question at a time; keep each
  option label to a couple of words.
- Before anything destructive (a commit, a shell command, a risky edit), show the
  exact command / message / one-line diff at HUD size, then ask for approval via
  `AskUserQuestion` (e.g. Run / Cancel). Don't act first and report after.

## When in doubt

Shorter. If you're unsure whether to include something, push it to the expansion
and offer `more?`.
