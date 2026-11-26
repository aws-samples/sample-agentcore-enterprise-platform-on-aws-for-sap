// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"

/**
 * `/deployment/settings`: what applies to every pack in this stack. The contact addresses
 * the SOPs name by role (`{{CONTACT_<KEY>}}`), and the facts of the deployment: its mode and
 * each SAP system's role. Not in `aws-exports.json`, which anyone can fetch.
 *
 * Distinct from `lib/config`'s `getConfig()`, which reads the deploy-time console config.
 */

export type SystemRole = "sandbox" | "development" | "quality" | "production"

export interface SapSystem {
  name: string
  system_role: SystemRole
}

export interface DeploymentFacts {
  mode: "workshop" | "production"
  promotion_role?: "development" | "quality" | "production"
  /**
   * True outside development: every pack and SOP write over HTTP is refused, and a pack
   * arrives only by an import. The console shows "This stack takes packs by promotion only".
   */
  promotion_only?: boolean
  sap_systems: SapSystem[]
  /** At least one system and none is production: the console shows the sandbox band. */
  non_production: boolean
}

export interface EffectiveContact {
  value: string | null
  kind: "contact"
  /** `platform.yaml` or `stack contact override`. */
  source: string
}

export interface DeploymentSettings {
  contacts: {
    /** Contact key → address, as deployed (`platform.yaml`): what an override reverts to. */
    defaults: Record<string, string>
    /** Only the contacts changed here. */
    overrides: Record<string, string>
    /** Keyed `CONTACT_<KEY>`, as a SOP cites it. */
    effective: Record<string, EffectiveContact>
  }
  deployment: DeploymentFacts
  /** What the caller may do across the deployment, from the rules the write routes use. */
  access?: { may_create_pack: boolean; may_edit_contacts: boolean }
  /** The last emergency stop, or null when this stack has had none. */
  last_stop?: DeploymentStop | null
}

/** One run an emergency stop reached: its grant is revoked whatever else took. */
export interface StoppedRun {
  run_id: string
  /** Null for a run with no case: a question, a rehearsal, a probe. */
  case_id: string | null
  /** The runtime agreed to stop the session. */
  session_stopped: boolean
  /** The case's `live_run` was still this run's, and is released. */
  released: boolean
  /** The case read `processing` and now reads `manual_review_required`. */
  parked: boolean
}

/**
 * `POST /deployment/stop` (6.3): the deployment's trigger mode set to manual, every run grant
 * revoked, each case's session stopped and the case parked for a person.
 */
export interface DeploymentStop {
  stopped_at: string
  /** "ops@example.com (8f0c…)": the caller and their subject. */
  stopped_by: string
  reason: string
  /** False when the trigger mode could not be written. */
  trigger_mode_set: boolean
  runs_total: number
  /** At most 100. */
  runs: StoppedRun[]
  /** False when the time budget ran out before every session stop was asked. */
  complete: boolean
  /**
   * True from the stop until an administrator clears it (7.2): while it stands, nothing new
   * starts. Clearing ends it; the trigger mode stays manual until someone sets it to auto.
   */
  stands: boolean
  /** The agent queue's consumer was paused, so queued work waits there until the clear. */
  queue_held?: boolean
  /** Who cleared the stop, when and why; absent while it stands. */
  cleared_at?: string
  cleared_by?: string
  clear_reason?: string
}

/** Ends a standing stop, with a reason. Admins only. The trigger mode stays manual. */
export async function clearStop(reason: string, token: string): Promise<{ stop: DeploymentStop }> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/deployment/stop/clear`,
    { token, method: "POST", body: { reason } },
    "Could not clear the stop",
    readApiError
  )
}

export async function fetchDeploymentSettings(token: string): Promise<DeploymentSettings> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/deployment/settings`,
    { token },
    "Could not read the deployment settings",
    readApiError
  )
}

/** `null` deletes an override, reverting the contact to its deployed address. */
export async function saveContacts(
  contacts: Record<string, string | null>,
  token: string
): Promise<{ updated: number; updated_by: string }> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/deployment/settings`,
    { token, method: "PUT", body: { contacts } },
    "Could not save the contacts",
    readApiError
  )
}

/** The emergency stop. Gated as the deployment's trigger mode is: admins only. */
export async function stopDeployment(
  reason: string,
  token: string
): Promise<{ stop: DeploymentStop }> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/deployment/stop`,
    { token, method: "POST", body: { reason } },
    "Could not stop the deployment",
    readApiError
  )
}
