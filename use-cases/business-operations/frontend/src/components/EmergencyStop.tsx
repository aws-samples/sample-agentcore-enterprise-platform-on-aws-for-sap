// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react"
import { Link } from "react-router"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { OctagonX } from "lucide-react"
import { toast } from "sonner"
import { useApi } from "@/hooks/useApi"
import { useDeploymentSettings } from "@/hooks/usePack"
import { AGENT_PULSE_KEY } from "@/hooks/useCaseCounts"
import { Button, buttonVariants } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { ReasonButton } from "@/components/ReasonButton"
import { When } from "@/components/When"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { fetchViewCounts } from "@/services/casesService"
import { clearStop, stopDeployment, type DeploymentStop } from "@/services/deploymentService"

/**
 * The emergency stop (`POST /deployment/stop`): the deployment's trigger mode to manual, every run's grant
 * revoked, and each case that was being worked parked for a person, in one action with a
 * reason. Beside the trigger mode on Deployment settings, and gated as it is. Until an
 * administrator clears it, nothing new starts and a band across the console says who stopped
 * it, when and why.
 */

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** What the route takes: 1 to 500 characters after trimming, no control character. */
const CONTROL = /[\u0000-\u001f\u007f]/
export function reasonProblem(reason: string, empty = "Say why you are stopping."): string | null {
  const r = reason.trim()
  if (!r) return empty
  if (r.length > 500) return "At most 500 characters."
  if (CONTROL.test(r)) return "One line, with no special characters."
  return null
}

/** "ops@example.com (8f0c…)" → "ops@example.com": the person, without their subject id. */
export function stopperName(stoppedBy: string): string {
  return stoppedBy.replace(/\s*\(.*\)\s*$/, "") || stoppedBy
}

/** A reason as a clause: its own closing full stop dropped, since the sentence adds one. */
export const asClause = (reason: string) => reason.trim().replace(/[.!?]+$/, "")

/**
 * Whether a stop stands, as the API says it (`last_stop.stands`, 7.2): from the stop until an
 * administrator clears it, whatever the trigger mode reads in between.
 */
export function stopStands(stop: DeploymentStop | null | undefined): stop is DeploymentStop {
  return !!stop?.stands
}

/** Every query a stop or its clearing changes. */
const STOP_KEYS = [["trigger-mode"], ["deployment-settings"], ["cases"], AGENT_PULSE_KEY]

export function EmergencyStop({ disabledReason }: { disabledReason: string | null }) {
  const getToken = useApi()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState("")
  // Read when the dialog opens, so the number is the one the person acts on.
  const live = useQuery({
    queryKey: ["cases", "counts", "emergency-stop"],
    queryFn: async () => fetchViewCounts({}, await getToken()),
    enabled: open,
    staleTime: 0,
  })
  const running = live.data?.views.running

  const problem = reasonProblem(reason)
  const stop = useMutation({
    mutationFn: async () => stopDeployment(reason.trim(), await getToken()),
    onSuccess: async ({ stop: result }) => {
      close()
      const runs = `${plural(result.runs_total, "run was", "runs were")} stopped`
      if (!result.trigger_mode_set)
        toast.error("Runs stopped, but the trigger mode may still be auto", {
          description: `${runs}. The trigger mode could not be written: set it to manual here.`,
        })
      else
        toast.success("Stopped", {
          description: `The trigger mode is manual and ${runs}.${
            result.complete
              ? ""
              : " Some sessions were not asked to stop in time; their runs can no longer reach SAP."
          }${
            result.queue_held === false
              ? " The queue could not be paused: work queued now may fail and wait in the dead-letter queue."
              : ""
          }`,
        })
      await Promise.all(STOP_KEYS.map(queryKey => queryClient.invalidateQueries({ queryKey })))
    },
    onError: e =>
      toast.error("Couldn't stop the deployment", { description: (e as Error).message }),
  })

  function close() {
    setOpen(false)
    setReason("")
  }

  return (
    <>
      <ReasonButton
        reason={disabledReason}
        variant="destructive"
        size="sm"
        onClick={() => setOpen(true)}
      >
        <OctagonX />
        Emergency stop
      </ReasonButton>

      <AlertDialog open={open} onOpenChange={o => !o && close()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Stop every run?</AlertDialogTitle>
            <AlertDialogDescription>
              This sets the trigger mode to manual for every pack and stops every run in progress,
              questions and rehearsals included.{" "}
              {live.isLoading
                ? "Counting the cases being worked…"
                : running == null
                  ? "The number of cases being worked could not be read."
                  : running === 0
                    ? "No case has a run in progress now."
                    : `${plural(running, "case has a run", "cases have a run")} in progress now, and will wait in Manual review for a person.`}{" "}
              Nothing new starts, by itself or from the console, until an administrator clears the
              stop here.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Field>
            <FieldLabel htmlFor="stop-reason">Reason</FieldLabel>
            <Input
              id="stop-reason"
              autoComplete="off"
              value={reason}
              maxLength={500}
              aria-invalid={(!!reason && !!problem) || undefined}
              onChange={e => setReason(e.target.value)}
            />
            <FieldDescription>
              {reason && problem
                ? problem
                : "One line. Shown to everyone in the console while the stop stands, and kept with the stop."}
            </FieldDescription>
          </Field>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={!!problem || stop.isPending}
              onClick={() => stop.mutate()}
            >
              {stop.isPending ? "Stopping…" : "Stop every run"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

/** The band across the console while a stop stands. */
export function StopBand() {
  const settings = useDeploymentSettings()
  const stop = settings.data?.last_stop
  if (!stopStands(stop)) return null
  return (
    <div
      role="region"
      aria-label="Emergency stop"
      className="flex min-h-8 flex-none items-center gap-2 border-b bg-danger-wash px-4 py-1 text-sm text-danger-ink"
    >
      <OctagonX className="size-3.5 flex-none" aria-hidden="true" />
      <span className="flex-none font-medium">Stopped</span>
      <span className="min-w-0">
        {stopperName(stop.stopped_by)} stopped every run <When at={stop.stopped_at} />:{" "}
        {asClause(stop.reason)}. Nothing new starts until an administrator clears the stop.
      </span>
      <Link
        to="/deployment"
        className={cn(buttonVariants({ variant: "ghost", size: "xs" }), "ml-auto flex-none")}
      >
        Deployment settings
      </Link>
    </div>
  )
}

/**
 * Clear the stop (`POST /deployment/stop/clear`, 7.2): runs may start again, from the console
 * and from SAP. The trigger mode stays manual; set it back to auto beside it.
 */
export function ClearStop({ disabledReason }: { disabledReason: string | null }) {
  const getToken = useApi()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState("")
  const problem = reasonProblem(reason, "Say why it is safe to start runs again.")
  const clear = useMutation({
    mutationFn: async () => clearStop(reason.trim(), await getToken()),
    onSuccess: async () => {
      close()
      toast.success("Stop cleared", {
        description: "Runs may start again. The trigger mode stays manual until you set it to auto.",
      })
      await Promise.all(STOP_KEYS.map(queryKey => queryClient.invalidateQueries({ queryKey })))
    },
    onError: e => toast.error("Couldn't clear the stop", { description: (e as Error).message }),
  })

  function close() {
    setOpen(false)
    setReason("")
  }

  return (
    <>
      <ReasonButton
        reason={disabledReason}
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
      >
        Clear the stop
      </ReasonButton>

      <AlertDialog open={open} onOpenChange={o => !o && close()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Clear the stop?</AlertDialogTitle>
            <AlertDialogDescription>
              People can start runs again: Process, questions and rehearsals, and the decisions that
              resume a case. The trigger mode stays manual; set it back to auto when
              you are ready. Cases the stop parked wait in Manual review until a person starts them.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Field>
            <FieldLabel htmlFor="clear-reason">Reason</FieldLabel>
            <Input
              id="clear-reason"
              autoComplete="off"
              value={reason}
              maxLength={500}
              aria-invalid={(!!reason && !!problem) || undefined}
              onChange={e => setReason(e.target.value)}
            />
            <FieldDescription>
              {reason && problem ? problem : "One line, kept with the stop."}
            </FieldDescription>
          </Field>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button disabled={!!problem || clear.isPending} onClick={() => clear.mutate()}>
              {clear.isPending ? "Clearing…" : "Clear the stop"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
