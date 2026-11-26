// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Class merging for Tailwind: shadcn's own `cn`, which the components in ui/ import too. */
export { cn } from "cn"

/**
 * A wire value, spelled the way a person would say it: `missing_goods_receipt` →
 * `missing goods receipt`.
 *
 * SAP exception and process types reach the record as enum tokens, and the operator was
 * being shown them raw. Display only — `lib/briefing.ts` and `lib/digest.ts` keep handing
 * the underscored value to the model and to the tests that pin it, because that is the
 * name the record actually uses.
 */
export function humanizeToken(value: string): string {
  return value.replace(/_/g, " ")
}
