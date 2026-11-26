// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The one place state colour is decided.
 *
 * Case status and ticket status previously carried their own independent colour
 * choices — cases on `-50`/`-700` weights, tickets on `-100`/`-800` plus emoji —
 * so the same meaning rendered differently depending on which screen you were
 * looking at. Both now map onto this vocabulary instead.
 *
 * Tones are semantic, not decorative: a tone answers "what does this state mean
 * to the operator", never "what looks nice here". Two states in the same
 * category may share a tone and be told apart by their label.
 *
 * **These now route through design tokens, not raw Tailwind palette weights.** An
 * ERP document has two outcomes — `held` (blocked or refused) and `released`
 * (posted, cleared) — plus ink for what is yours and the warm neutral for anything
 * that is not. Six tone keys, four token families: the vocabulary stayed the same
 * size so no call site moved, but the palette stopped carrying six competing hues
 * that each claimed a different kind of importance.
 *
 * **`attention` is ink, not red.** A case stopped for a human is the supervised path
 * working, not a fault — the agent is *supposed* to hand those over, and a queue of
 * them is the normal state of this product. Painting it red is what makes a console
 * look alarming while telling the operator nothing. On a warm paper ground the
 * darkest mark is what pulls the eye anyway, so ink does the job that red was doing
 * badly, and `held` stays reserved for the one thing that really is a failure.
 * `TriggerModeGovernor`'s readout depends on this: it prints "N stopped for a human as
 * the SOP requires" beside "N failed" in one sentence, and those two must not render
 * alike.
 *
 * **`neutral`, `info` and `progress` share the soft ink, and that stays.** It was on the
 * list to resolve; measuring it is what settled it the other way, so the two answers that
 * look obvious are written down here rather than re-derived:
 *
 *   - *Not a third ink step.* `--subtle-foreground` is the only one left, and it is a 3:1
 *     graphical token — 3.48:1 on paper, 3.27:1 on muted. `TONE_TEXT` and `TONE_BADGE`
 *     promise 4.5:1, and globals.css says of that token in as many words: do not set text
 *     in it. Distinguishing two tones by underpainting one of them is not a fix.
 *   - *Not the `--agent` violet, tempting as "work under way" makes it sound.* `progress`
 *     is worn by `TicketPriority.Medium`, by `AgentAlert`'s warning level, by a short
 *     quantity variance and by a degraded health check. Violet is reserved for "the agent
 *     is working" and nothing else, so pointing this key at it would paint a
 *     medium-priority ticket as agent motion.
 *
 * The second bullet is the actual finding: this key is overloaded, not miscoloured. Giving
 * the agent-motion states their own tone means re-partitioning every `progress` call site,
 * which is a semantic change to make deliberately — not a colour cleanup. Until then the
 * shared ink is honest: none of the three is an outcome anyone acts on from a hue, and
 * `TONE_DOT` below already separates all three where there is no label to read.
 *
 * The other consequence worth knowing: there are no `dark:` variants here any more.
 * `--held` and `--released` are re-declared in `.dark` (see styles/globals.css), so
 * the theme is a token flip and a tone cannot drift between the two grounds. Add
 * colours here, never in a component — and add them as tokens, never as weights.
 */
export type StatusTone =
  /** Terminal and inert — nothing is expected to happen next. */
  | "neutral"
  /** Known and queued, no work started. */
  | "info"
  /** Work under way, no human needed. */
  | "progress"
  /** Blocked on a human decision. */
  | "attention"
  /** Finished well. */
  | "success"
  /** Failed, refused, or needs escalation. */
  | "danger"

/**
 * Pill background and text. `attention` is the only tone whose pill inverts — a
 * solid ink chip, because it is the one state that is asking for something.
 */
export const TONE_BADGE: Record<StatusTone, string> = {
  neutral: "bg-muted text-muted-foreground",
  info: "bg-muted text-muted-foreground",
  progress: "bg-muted text-muted-foreground",
  attention: "bg-foreground text-background",
  success: "bg-success-wash text-success-ink",
  danger: "bg-danger-wash text-danger-ink",
}

/** Full-width notice with a left accent — errors, warnings, and inline prompts. */
export const TONE_BANNER: Record<StatusTone, string> = {
  neutral: "border-border bg-muted text-muted-foreground",
  info: "border-border bg-muted text-muted-foreground",
  progress: "border-muted-foreground/40 bg-muted text-muted-foreground",
  attention: "border-foreground bg-muted text-foreground",
  success: "border-success bg-success-wash text-success-ink",
  danger: "border-danger bg-danger-wash text-danger-ink",
}

/**
 * Standalone text/icon colour — a lucide icon or a count with no pill around it.
 * Every value here clears 4.5:1 on both grounds because it renders as text, not
 * only as an icon; that floor is why `--tone-danger-text` and `--tone-success-text` are
 * darker in light and lighter in dark than the fills.
 */
export const TONE_TEXT: Record<StatusTone, string> = {
  neutral: "text-muted-foreground",
  info: "text-muted-foreground",
  progress: "text-muted-foreground",
  attention: "text-foreground",
  success: "text-success-text",
  danger: "text-danger-text",
}

/**
 * Leading dot, used both inside badges and standalone in filter menus. A dot is a
 * graphical object, so it only owes 3:1 — which is what lets the four soft tones be
 * told apart here even though their text colour is shared. At 6px there is no room
 * for a hollow ring, so `info` is a lighter fill of the same ink rather than an
 * outline: not yours yet, versus in flight, versus yours.
 */
export const TONE_DOT: Record<StatusTone, string> = {
  neutral: "bg-muted-foreground/40",
  info: "bg-muted-foreground/70",
  progress: "bg-muted-foreground",
  attention: "bg-foreground",
  success: "bg-success",
  danger: "bg-danger",
}
