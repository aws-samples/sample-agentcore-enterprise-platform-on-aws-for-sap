// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { indexPacks, type FieldType, type PackIndex, type UseCasePack } from "@/lib/registry"
import type { Registry } from "@/hooks/useRegistry"
import apPack from "./fixtures/finance_ops.pack.json"

/**
 * The AP pack as `GET /registry` would serve it, joined here from the real config.
 *
 * Every surface that renders a case now reads its labels, its money field and its reason
 * field from the registry, so a test of that surface needs a pack. Built from
 * `packs/finance_ops/pack.json` rather than hand-written, because a fixture that drifts
 * from the config stops testing what ships. Mirrors `public_view()` in
 * `api/layer/python/registry.py` — the one place that join is defined.
 */
export function packFromConfig(): UseCasePack {
  const apSource = apPack.source
  const fieldMap = apSource.field_map as Record<string, { path: string }>
  const declared = apPack.fields as Record<
    string,
    { label: string; type: string; currency_field?: string; role?: string; panel?: boolean }
  >
  return {
    id: "finance_ops",
    display_name: apPack.display_name,
    description: apPack.description,
    label: apSource.label,
    short_id: apSource.short_id,
    process_types: Object.keys(apPack.process_type_to_sop).sort(),
    source: {
      kind: apSource.kind,
      service: apSource.service,
      entity: apSource.entity,
      // Off the real pack, so a reordered `source` block reorders every row these tests
      // render — the order is published data, not a constant a fixture may invent.
      key: apSource.key.map(k => k.field),
    },
    fields: Object.entries(declared).map(([attribute, spec]) => ({
      attribute,
      label: spec.label,
      type: spec.type as FieldType,
      sap_field: fieldMap[attribute]?.path ?? "",
      ...(spec.currency_field ? { currency_field: spec.currency_field } : {}),
      ...(spec.role ? { role: spec.role as "reason" } : {}),
      ...(spec.panel === false ? { panel: false } : {}),
    })),
  }
}

export const AP_PACK: UseCasePack = packFromConfig()

/** The registry a single-pack deployment serves, ready to pass as `packs`. */
export const AP_PACKS: PackIndex = indexPacks([AP_PACK])

/** What `useRegistry()` returns for that deployment, for tests that mock the hook. */
export const AP_REGISTRY: Registry = {
  packs: [AP_PACK],
  byId: AP_PACKS,
  scopes: [AP_PACK],
  resolved: true,
  error: null,
}
