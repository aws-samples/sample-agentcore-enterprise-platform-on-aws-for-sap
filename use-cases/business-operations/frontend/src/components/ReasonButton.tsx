// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ComponentProps } from "react"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

/**
 * A button that may be unavailable to this person, and then says why. Disabled with a reason
 * it stays focusable (`aria-disabled`), so the tooltip reaches keyboard and touch users too,
 * and a click does nothing. Only the control is dimmed; the content around it is not.
 */
export function ReasonButton({
  reason,
  className,
  ...props
}: ComponentProps<typeof Button> & { reason?: string | null }) {
  if (!reason) return <Button className={className} {...props} />
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            {...props}
            className={["aria-disabled:opacity-50", className].filter(Boolean).join(" ")}
            disabled
            focusableWhenDisabled
          />
        }
      />
      <TooltipContent>{reason}</TooltipContent>
    </Tooltip>
  )
}
