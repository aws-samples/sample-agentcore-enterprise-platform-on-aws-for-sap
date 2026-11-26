// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { UseCasePack } from "@/lib/registry"
import { getConfig } from "@/lib/config"
import { apiFetch } from "@/lib/apiFetch"

/**
 * Which use cases this deployment runs, scoped to what the caller may see.
 *
 * Served out of the published registry, so a pack published a minute ago is here —
 * this build was never compiled against a list of packs.
 */
export async function fetchRegistry(token: string): Promise<UseCasePack[]> {
  const { apiUrl } = await getConfig()
  const body = await apiFetch<{ packs?: UseCasePack[] }>(
    `${apiUrl}/registry`,
    { token },
    "Failed to fetch use case registry"
  )
  return body.packs ?? []
}
