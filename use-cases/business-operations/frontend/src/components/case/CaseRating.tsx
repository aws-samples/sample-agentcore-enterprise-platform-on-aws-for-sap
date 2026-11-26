// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react"
import { ThumbsDown, ThumbsUp } from "lucide-react"
import { submitCaseRating } from "@/services/casesService"
import { Toggle } from "@/components/ui/toggle"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { TONE_TEXT } from "@/lib/statusTone"
import { UserRating, type CaseRating as Rating } from "@/types/cases"

type Verdict = "positive" | "negative"

/** Positive raters minus negative ones — the same score the exemplar builder ranks on. */
export function ratingScore(ratings: Record<string, Rating> | undefined): number {
  return Object.values(ratings ?? {}).reduce(
    (n, r) =>
      n + (r.rating === UserRating.Positive ? 1 : r.rating === UserRating.Negative ? -1 : 0),
    0
  )
}

/**
 * Rate the agent's work on a case, and see who else did.
 *
 * A rating is a vote on whether this case should teach the agent: the exemplar builder
 * ranks completed cases by net rating and never uses one scored below zero. One vote per
 * operator; pressing the other thumb changes yours.
 */
export function CaseRating({
  caseId,
  ratings,
  mySub,
  token,
  onSaved,
}: {
  caseId: string
  ratings?: Record<string, Rating>
  /** The signed-in operator's `sub`, to show which thumb is theirs. */
  mySub?: string
  token: string
  onSaved: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const mine: string | undefined = mySub ? ratings?.[mySub]?.rating : undefined
  const raters = Object.values(ratings ?? {}).sort((a, b) => b.at.localeCompare(a.at))
  const score = ratingScore(ratings)

  const rate = async (verdict: Verdict) => {
    setBusy(true)
    setError(null)
    try {
      await submitCaseRating(caseId, verdict, undefined, token)
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to submit rating")
    } finally {
      setBusy(false)
    }
  }

  const thumb = (verdict: Verdict) => {
    const Icon = verdict === "positive" ? ThumbsUp : ThumbsDown
    const label = verdict === "positive" ? "Good precedent" : "Bad precedent"
    return (
      <Tooltip key={verdict}>
        <TooltipTrigger
          render={
            <Toggle
              size="sm"
              pressed={mine === verdict}
              onPressedChange={() => rate(verdict)}
              disabled={busy}
              aria-label={label}
            />
          }
        >
          <Icon />
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-sm font-medium">Rating</h3>
      <div className="flex items-center gap-1 text-sm">
        {thumb("positive")}
        {thumb("negative")}
        <span className="ml-2 tabular-nums text-muted-foreground">
          {raters.length ? `${score > 0 ? "+" : ""}${score} from ${raters.length}` : "Not rated"}
        </span>
      </div>
      {raters.length > 0 && (
        <ul className="flex flex-col gap-0.5 text-xs text-muted-foreground">
          {raters.map(r => (
            <li key={`${r.name}-${r.at}`}>
              {r.rating === UserRating.Positive ? "Up" : "Down"} · {r.name}
              {r.comment ? `: ${r.comment}` : ""}
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className={`text-xs ${TONE_TEXT.danger}`}>
          {error}
        </p>
      )}
    </div>
  )
}
