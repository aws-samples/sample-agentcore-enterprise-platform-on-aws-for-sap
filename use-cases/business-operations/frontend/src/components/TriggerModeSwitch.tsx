// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { useApi } from "@/hooks/useApi"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Field, FieldLabel } from "@/components/ui/field"
import { ReasonButton } from "@/components/ReasonButton"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import type { TriggerMode } from "@/services/triggerModeService"

/**
 * The control that sets a trigger mode to auto or manual: the deployment's, or one
 * pack's. The only two places that can arm auto use this file.
 *
 * The friction is asymmetric by design: turning auto off is one click, because a mode that is
 * hard to leave is a worse failure than one that is hard to enter. Turning it on means typing
 * the word.
 *
 * A person who may not change it sees the button disabled with the reason, still focusable.
 */

/** What the operator types to arm auto. Not "yes": it should name the thing. */
const CONFIRM_WORD = "AUTO"

export function TriggerModeSwitch({
  mode,
  save,
  invalidate,
  disabledReason,
  scope,
}: {
  mode: TriggerMode | null | undefined
  save: (next: TriggerMode, token: string) => Promise<unknown>
  /** Query keys to refetch after a change. */
  invalidate: readonly (readonly unknown[])[]
  /** Why this person cannot change it, or null when they can. */
  disabledReason: string | null
  /** What turning it on starts, in one sentence. */
  scope: string
}) {
  const getToken = useApi()
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const [typed, setTyped] = useState("")

  const mutation = useMutation({
    mutationFn: async (next: TriggerMode) => save(next, await getToken()),
    onSuccess: async () => {
      closeDialog()
      await Promise.all(invalidate.map(queryKey => queryClient.invalidateQueries({ queryKey })))
    },
    onError: e =>
      toast.error("Couldn't change the trigger mode", { description: (e as Error).message }),
  })

  function closeDialog() {
    setConfirming(false)
    setTyped("")
  }

  const on = mode === "auto"
  const label = on ? "Set to manual" : "Set to auto"

  return (
    <>
      <ReasonButton
        reason={disabledReason}
        variant={on ? "outline" : "default"}
        size="sm"
        disabled={mutation.isPending}
        onClick={() => {
          if (on) mutation.mutate("manual")
          else setConfirming(true)
        }}
      >
        {mutation.isPending ? "Saving…" : label}
      </ReasonButton>

      <AlertDialog open={confirming} onOpenChange={open => !open && closeDialog()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Let the agent start without a person?</AlertDialogTitle>
            <AlertDialogDescription>
              {scope} The agent follows its SOPs and writes to SAP within the limits; a change above
              them still waits for an approver. Each run is billed.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Field>
            <FieldLabel htmlFor="confirm-auto">
              Type <span className="font-mono font-semibold">{CONFIRM_WORD}</span> to confirm
            </FieldLabel>
            <Input
              id="confirm-auto"
              autoComplete="off"
              value={typed}
              onChange={e => setTyped(e.target.value)}
            />
          </Field>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              disabled={typed !== CONFIRM_WORD || mutation.isPending}
              onClick={() => mutation.mutate("auto")}
            >
              {mutation.isPending ? "Saving…" : "Set to auto"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
