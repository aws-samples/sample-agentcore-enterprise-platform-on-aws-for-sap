// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { limitOwners } from "@/services/controlsService"

describe("limitOwners", () => {
  it("names the stack group, and the pack's own groups where it narrows them", () => {
    expect(limitOwners({ entitlement: { groups: ["a"] } })).toBe(
      "members of business-operations-limit-owners"
    )
    expect(limitOwners({ entitlement: null })).toBe("members of business-operations-limit-owners")
    expect(
      limitOwners({
        entitlement: { groups: ["a"], limit_groups: ["ap-controllers", "ap-leads"] },
      })
    ).toBe("members of business-operations-limit-owners who are also in ap-controllers or ap-leads")
  })
})
