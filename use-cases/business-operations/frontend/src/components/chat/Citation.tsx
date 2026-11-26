// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Link } from "react-router"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { Citation as CitationData } from "@/types/cases"
import { cn } from "@/lib/utils"

/**
 * A span the agent claims to have quoted, marked the way an auditor marks a page.
 *
 * `--marker` is the one bold colour in the design and this is the only thing that
 * spends it: a verified quote gets the highlighter, so "the agent's reasoning is
 * checkable" is legible across the room rather than one click deep.
 *
 * **An unverified quote is not an accusation.** `verified: false` means the span is not
 * in the text this run was given — which catches a compliant agent that paraphrased
 * just as readily as one that invented a rule (see the generated type's own warning).
 * So it renders as an unfinished mark, a dotted underline, and never in `held`. Red
 * here would report a violation the data does not support.
 */
/**
 * What a quote's mark says on hover and focus. Deliberately not "where this quote is from":
 * the link resolves the SOP the run was *given*, which is the same document in every case
 * the router routed, but the baseline a quote is graded against also absorbs whatever the
 * agent looked up mid-run, and nothing records which document that was (`sop_key` in the
 * schema).
 */
export function citationNote(verified: boolean, linked: boolean): string {
  const verdict = verified
    ? "Quoted from the text this run was given."
    : "Not found in the text this run was given. It may be a paraphrase rather than a departure from the SOP."
  return linked ? `${verdict} Opens the SOP this run was given.` : verdict
}

export function Citation({
  citation,
  href,
  className,
}: {
  citation: CitationData
  /**
   * Where the quoted text can be read in full — `sopLink`, or omitted when the run
   * recorded no SOP to open.
   *
   * A `<Link>` rather than the `onClick` this used to declare and nobody passed: it is a
   * navigation, so ctrl-click and "open in new tab" have to work — an operator reading a
   * quote wants the SOP *beside* the case, not instead of it.
   */
  href?: string
  className?: string
}) {
  const title = citationNote(citation.verified, Boolean(href))

  const marked = cn(
    "text-inherit",
    citation.verified
      ? // The wipe animates `background-size`, so the gradient and `no-repeat` have to be
        // declared here rather than in the keyframe — see the marker note in globals.css.
        // `box-decoration-clone` keeps the mark whole when the quote wraps a line.
        "box-decoration-clone bg-marker bg-no-repeat animate-marker-wipe"
      : "bg-transparent underline decoration-dotted decoration-muted-foreground underline-offset-2",
    className
  )

  // `inline`, so a quote inside a paragraph still wraps across lines like the prose
  // around it instead of becoming an inline-block brick.
  if (href) {
    return (
      <Tooltip>
        <TooltipTrigger render={<Link to={href} className="inline text-left" />}>
          <mark className={cn(marked, "cursor-pointer")}>{citation.quote}</mark>
        </TooltipTrigger>
        <TooltipContent>{title}</TooltipContent>
      </Tooltip>
    )
  }

  return (
    <Tooltip>
      <TooltipTrigger render={<mark className={marked} />} tabIndex={0}>
        {citation.quote}
      </TooltipTrigger>
      <TooltipContent>{title}</TooltipContent>
    </Tooltip>
  )
}
