// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import { toast } from "sonner"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { TriggerModeSwitch } from "@/components/TriggerModeSwitch"
import {
  ClearStop,
  EmergencyStop,
  asClause,
  stopStands,
  stopperName,
} from "@/components/EmergencyStop"
import { When } from "@/components/When"
import { useDeploymentSettings } from "@/hooks/usePack"
import { useApi } from "@/hooks/useApi"
import { useIsAdmin } from "@/hooks/useIsAdmin"
import { fetchDeploymentTriggerMode, saveDeploymentTriggerMode } from "@/services/triggerModeService"
import { fetchDeploymentSettings, saveContacts } from "@/services/deploymentService"
import { humanise } from "@/lib/registry"
import { systemRoleWords } from "@/components/SandboxMarker"

/**
 * What applies to every pack in this deployment (decision 17): the deployment's
 * trigger mode with the emergency stop beside it, and the contacts the procedures name. A pack's own settings, such as its
  * trigger mode and lowered limits, are on that pack's page.
 *
 * Everyone can read this page; changing it needs the admins group, and the API decides.
 */
export default function DeploymentSettingsPage() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="flex max-w-2xl flex-col gap-6 p-6 pb-ask">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Deployment settings</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Settings that apply to every pack. A pack&apos;s own settings are on its page.
          </p>
        </div>
        <TriggerModeCard />
        <Contacts />
        <Connections />
      </div>
    </div>
  )
}

function AccessBadge({ may }: { may: boolean }) {
  return <Badge variant={may ? "default" : "secondary"}>{may ? "You can edit" : "Read only"}</Badge>
}

function TriggerModeCard() {
  const auth = useAuth()
  const getToken = useApi()
  const admin = useIsAdmin()
  const query = useQuery({
    queryKey: ["trigger-mode", "deployment"],
    queryFn: async () => fetchDeploymentTriggerMode(await getToken()),
    enabled: auth.isAuthenticated,
  })
  const lastStop = useDeploymentSettings().data?.last_stop
  const mode = query.data?.["trigger-mode"]
  const label = mode === "auto" ? "auto" : mode === "manual" ? "manual" : "Not set, so manual"

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Trigger mode <AccessBadge may={admin} />
        </CardTitle>
        <CardDescription>
          The deployment's trigger mode. While it is manual, no pack runs on its own, whatever the
          pack's trigger mode; a person starts each run. Containment sets it to manual on an alarm.
          Each pack has its own trigger mode on its page. The emergency stop sets it to manual and
          stops every run in progress, and nothing new starts until an administrator clears it.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {query.isLoading ? (
          <Skeleton className="h-8 w-48" />
        ) : query.isError ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t read the trigger mode</AlertTitle>
            <AlertDescription>{(query.error as Error).message}</AlertDescription>
          </Alert>
        ) : (
          <>
            <div className="flex items-center gap-4">
              <span className="mr-auto text-sm">
                Now: <span className="font-medium">{label}</span>
              </span>
              <EmergencyStop
                disabledReason={!admin ? "Only an administrator uses the emergency stop." : null}
              />
              {stopStands(lastStop) && (
                <ClearStop
                  disabledReason={!admin ? "Only an administrator clears the stop." : null}
                />
              )}
              <TriggerModeSwitch
                mode={mode}
                save={saveDeploymentTriggerMode}
                invalidate={[["trigger-mode"]]}
                scope="Each pack whose own trigger mode is auto then starts the agent on every new case."
                disabledReason={
                  !admin
                    ? "Only an administrator changes the trigger mode."
                    : stopStands(lastStop)
                      ? "An emergency stop stands. Clear the stop first, then set the trigger mode to auto."
                      : query.data?.["autonomous-capable"] === false
                        ? "This deployment's sign-in profile has no unattended caller."
                        : null
                }
              />
            </div>
            {lastStop && (
              <p className="text-sm text-muted-foreground">
                {lastStop.stands ? "The emergency stop stands" : "Last emergency stop"}:{" "}
                {stopperName(lastStop.stopped_by)}, <When at={lastStop.stopped_at} />.{" "}
                {asClause(lastStop.reason)}.{" "}
                {lastStop.runs_total === 1 ? "1 run" : `${lastStop.runs_total} runs`} stopped.
                {lastStop.stands
                  ? " Nothing new starts until an administrator clears it."
                  : lastStop.cleared_by && (
                      <>
                        {" "}
                        Cleared by {stopperName(lastStop.cleared_by)}
                        {lastStop.cleared_at && (
                          <>
                            , <When at={lastStop.cleared_at} />
                          </>
                        )}
                        {lastStop.clear_reason ? `: ${asClause(lastStop.clear_reason)}` : ""}.
                      </>
                    )}
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function Contacts() {
  const auth = useAuth()
  const getToken = useApi()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ["deployment-settings"],
    queryFn: async () => fetchDeploymentSettings(await getToken()),
    enabled: auth.isAuthenticated,
  })
  // The API's answer (`access.may_edit_contacts`): the authors group, in any stack.
  const mayEdit = !!query.data?.access?.may_edit_contacts
  const effective = useMemo(() => {
    const c = query.data
    if (!c) return {}
    return Object.fromEntries(
      Object.keys(c.contacts.defaults).map(k => [
        k,
        c.contacts.overrides[k] ?? c.contacts.defaults[k] ?? "",
      ])
    ) as Record<string, string>
  }, [query.data])
  const [draft, setDraft] = useState<Record<string, string>>({})
  useEffect(() => setDraft(effective), [effective])
  const changed = Object.keys(draft).filter(k => draft[k] !== effective[k])

  const save = useMutation({
    mutationFn: async () => {
      const contacts = Object.fromEntries(
        changed.map(k => [
          k,
          draft[k].trim() === "" || draft[k] === query.data?.contacts.defaults[k]
            ? null
            : draft[k].trim(),
        ])
      )
      return saveContacts(contacts, await getToken())
    },
    onSuccess: async () => {
      toast.success(`Saved ${changed.length} contact${changed.length === 1 ? "" : "s"}`)
      await queryClient.invalidateQueries({ queryKey: ["deployment-settings"] })
    },
    onError: e => toast.error("Couldn't save the contacts", { description: (e as Error).message }),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Contacts <AccessBadge may={mayEdit} />
        </CardTitle>
        <CardDescription>
          Who the SOPs name when they escalate or ask a question. Empty means the deployed value
          from the use case&apos;s config.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {query.isLoading ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
          </div>
        ) : query.isError ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t read the contacts</AlertTitle>
            <AlertDescription>{(query.error as Error).message}</AlertDescription>
          </Alert>
        ) : (
          <form
            onSubmit={e => {
              e.preventDefault()
              save.mutate()
            }}
          >
            <FieldGroup>
              {Object.keys(draft).map(key => (
                <Field key={key}>
                  <FieldLabel htmlFor={`contact-${key}`}>{humanise(key)}</FieldLabel>
                  <Input
                    id={`contact-${key}`}
                    type="email"
                    value={draft[key]}
                    readOnly={!mayEdit}
                    placeholder={query.data?.contacts.defaults[key]}
                    onChange={e => setDraft({ ...draft, [key]: e.target.value })}
                  />
                  {query.data?.contacts.overrides[key] && (
                    <FieldDescription>
                      Changed here; deployed value {query.data.contacts.defaults[key] || "none"}
                    </FieldDescription>
                  )}
                </Field>
              ))}
              {mayEdit && (
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    disabled={!changed.length}
                    onClick={() => setDraft(effective)}
                  >
                    Discard
                  </Button>
                  <Button type="submit" disabled={!changed.length || save.isPending}>
                    {save.isPending ? "Saving…" : "Save"}
                  </Button>
                </div>
              )}
            </FieldGroup>
          </form>
        )}
      </CardContent>
    </Card>
  )
}

/** The SAP systems this deployment reaches, and its mode. Set in `platform.yaml`, read only here. */
function Connections() {
  const auth = useAuth()
  const getToken = useApi()
  const query = useQuery({
    queryKey: ["deployment-settings"],
    queryFn: async () => fetchDeploymentSettings(await getToken()),
    enabled: auth.isAuthenticated,
  })
  const facts = query.data?.deployment
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Connections <Badge variant="secondary">Read only</Badge>
        </CardTitle>
        <CardDescription>
          The SAP systems the packs read and write, set in the deployment&apos;s config.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {!facts ? (
          <Skeleton className="h-9 w-full" />
        ) : (
          <dl className="flex flex-col gap-2 text-sm">
            <div className="flex gap-6">
              <dt className="w-40 flex-none text-muted-foreground">Deployment mode</dt>
              <dd>{facts.mode === "production" ? "Production" : "Workshop"}</dd>
            </div>
            {facts.promotion_role && (
              <div className="flex gap-6">
                <dt className="w-40 flex-none text-muted-foreground">Takes pack edits</dt>
                <dd>
                  {facts.promotion_role === "development"
                    ? "Yes, this is a development stack"
                    : "No, only promoted versions"}
                </dd>
              </div>
            )}
            {facts.sap_systems.map(s => (
              <div key={s.name} className="flex gap-6">
                <dt className="w-40 flex-none text-muted-foreground">SAP system</dt>
                <dd>
                  <span className="font-mono">{s.name}</span>, {systemRoleWords(s.system_role)}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </CardContent>
    </Card>
  )
}
