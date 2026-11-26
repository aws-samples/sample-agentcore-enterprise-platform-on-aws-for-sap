// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import axe from "axe-core"

/**
 * axe's violations under `node`, one line each, so a failing assertion names the rule and
 * the element. Colour contrast is off: jsdom computes no styles, so axe cannot measure it.
 */
export async function axeViolations(node: Element): Promise<string[]> {
  const { violations } = await axe.run(node, { rules: { "color-contrast": { enabled: false } } })
  return violations.map(v => `${v.id}: ${v.nodes.map(n => n.target.join(" ")).join(", ")}`)
}
