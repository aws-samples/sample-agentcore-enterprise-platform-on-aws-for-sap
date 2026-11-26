// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { formatStamp, shortAge, timeAgo } from "@/lib/timeAgo"

/**
 * A time as every page shows it: "12 min ago" within a day, then "Nov 19", with the full time
 * in a tooltip. `age` shows how long it has waited instead ("3h"), for a table column. In a
 * table the cell is not a tab stop (`focusable={false}`); elsewhere keyboard focus opens the
 * tooltip too.
 */
export function When({
  at,
  age = false,
  focusable = true,
  className,
}: {
  at?: string | null
  age?: boolean
  focusable?: boolean
  className?: string
}) {
  if (!at) return null
  return (
    <Tooltip>
      <TooltipTrigger
        render={<time dateTime={at} className={className} />}
        tabIndex={focusable ? 0 : undefined}
      >
        {age ? shortAge(at) : timeAgo(at)}
      </TooltipTrigger>
      <TooltipContent>{formatStamp(at)}</TooltipContent>
    </Tooltip>
  )
}
