// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Link } from "react-router"
import { Check, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { usePreference } from "@/hooks/usePreference"
import { useDeploymentTriggerMode, useDeploymentSettings } from "@/hooks/usePack"
import { cn } from "@/lib/utils"

export const FIRST_RUN_KEY = "ui.firstRun.dismissed"

interface Step {
  label: string
  done: boolean
  to?: string
  /** Where the step is done when it is not in the console. */
  hint?: string
}

/**
 * The steps to a first case on a new deployment (preview 1.5, frame 5; decision 19): one line
 * above the views, each step read from what is true now. It goes once a case is done, or when
 * the person dismisses it in this browser.
 */
export function FirstRunBanner({
  total,
  started,
  done,
}: {
  /** Cases the person can see. */
  total: number
  /** Cases the agent has started on. */
  started: number
  /** Cases done. */
  done: number
}) {
  const [dismissed, setDismissed] = usePreference<boolean>(FIRST_RUN_KEY, false)
  const settings = useDeploymentSettings()
  const triggerMode = useDeploymentTriggerMode()
  if (dismissed || done > 0 || !settings.data) return null

  const auto = triggerMode.data?.["trigger-mode"] === "auto"
  const steps: Step[] = [
    {
      label: "Connect SAP",
      done: (settings.data.deployment.sap_systems ?? []).length > 0,
      to: "/deployment",
    },
    {
      label: "Seed an exception",
      done: total > 0,
      hint: "Post a test exception in the sandbox with ops.py seed, or wait for the poller to find one.",
    },
    { label: "Start the first case", done: started > 0, to: "/cases?view=not-started" },
    { label: "Decide on it", done: false, to: "/cases?view=needs-you" },
    { label: "Set the trigger mode to auto", done: auto, to: "/deployment" },
  ]
  const current = steps.findIndex(s => !s.done)

  return (
    <section
      aria-label="First case"
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border bg-card px-4 py-2 text-sm"
    >
      <span className="font-medium">First case</span>
      <ol className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {steps.map((s, i) => (
          <li
            key={s.label}
            aria-current={i === current ? "step" : undefined}
            className={cn(
              "flex items-center gap-1.5",
              s.done ? "text-muted-foreground" : i === current ? "font-medium" : ""
            )}
          >
            <span
              aria-hidden="true"
              className={cn(
                "flex size-4 items-center justify-center rounded-full border text-xs tabular-nums",
                s.done && "border-success bg-success text-background"
              )}
            >
              {s.done ? <Check className="size-3" /> : i + 1}
            </span>
            <StepLabel step={s} />
            {s.done && <span className="sr-only">(done)</span>}
          </li>
        ))}
      </ol>
      <span className="ml-auto text-xs text-muted-foreground">Goes once a case is done</span>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Dismiss the first case steps"
        onClick={() => setDismissed(true)}
      >
        <X />
      </Button>
    </section>
  )
}

function StepLabel({ step }: { step: Step }) {
  if (step.done) return <span>{step.label}</span>
  if (step.to)
    return (
      <Link to={step.to} className="underline-offset-4 hover:underline">
        {step.label}
      </Link>
    )
  if (step.hint)
    return (
      <Tooltip>
        <TooltipTrigger
          render={<Button variant="link" size="sm" className="h-auto p-0 text-inherit" />}
        >
          {step.label}
        </TooltipTrigger>
        <TooltipContent>{step.hint}</TooltipContent>
      </Tooltip>
    )
  return <span>{step.label}</span>
}
