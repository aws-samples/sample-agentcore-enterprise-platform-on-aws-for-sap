// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Runtime configuration from /aws-exports.json, which the use case's CDK stack writes at
 * deploy time. Fetched once and cached for the lifetime of the app; a failed fetch is not
 * cached, so the next caller retries instead of the tab needing a reload.
 */

export interface AppConfig {
  authority: string
  /** External-IdP discovery URL (mutually exclusive with a Cognito authority). */
  metadataUrl?: string
  client_id: string
  redirect_uri: string
  post_logout_redirect_uri: string
  response_type: string
  scope: string
  automaticSilentRenew: boolean
  awsRegion: string
  /** The console API, `/tickets` included when sample ticketing is on. */
  apiUrl: string
  /** AppSync Events HTTP domain. Doubles as the WebSocket handshake's host header. */
  runEventsHttpDomain?: string
  /** AppSync Events realtime domain. Absent means this deployment has no push channel. */
  runEventsRealtimeDomain?: string
  /** True when the sample ticketing routes are mounted. Gates the Tickets UI. */
  ticketingEnabled: boolean
  /** The group whose members see the monitoring links. */
  adminsGroup: string
  /** The use case's CloudWatch dashboard, linked from the rail for admins. */
  dashboardUrl?: string
  /** AgentCore's agent observability view in the CloudWatch console, linked beside it. */
  agentObservabilityUrl?: string
  /**
   * "access" under the platform's direct Entra mode: the API and the run channel take
   * the access token (addressed to the platform app, carrying its app roles), not the
   * id token. Absent: the id token, as on Cognito.
   */
  apiToken?: "access"
  /** Console feature flags from the use case's config. A flag shows a view; it grants nothing. */
  features?: { test_data?: boolean }
  /** What the console calls itself (`console.brand`); `src/brand.ts` when absent. */
  brand?: { name: string; subtitle: string; monogram?: string | null }
}

let cached: AppConfig | null = null
let pending: Promise<AppConfig> | null = null

const strip = (url: string | undefined) => url?.replace(/\/+$/, "")

export function getConfig(): Promise<AppConfig> {
  if (cached) return Promise.resolve(cached)
  if (pending) return pending

  pending = fetch("/aws-exports.json")
    .then(res => {
      if (!res.ok) throw new Error(`Failed to load aws-exports.json: ${res.status}`)
      return res.json()
    })
    .then(raw => {
      cached = {
        ...raw,
        apiUrl: strip(raw.apiUrl) ?? "",
        ticketingEnabled: Boolean(raw.ticketingEnabled),
        metadataUrl: raw.metadata_url ?? raw.metadataUrl,
      }
      return cached!
    })
    .catch(error => {
      pending = null
      throw error
    })

  return pending
}
