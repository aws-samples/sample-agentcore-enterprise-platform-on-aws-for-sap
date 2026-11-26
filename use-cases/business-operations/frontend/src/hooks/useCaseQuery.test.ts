// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { caseListState, toCaseQuery } from "@/hooks/useCaseQuery"

const query = (search: string) => toCaseQuery(caseListState(new URLSearchParams(search)))

describe("The Cases list's server query", () => {
  it("maps the console's views to the route's", () => {
    expect(query("").view).toBe("needs_you")
    expect(query("view=not-started").view).toBe("new")
    expect(query("view=working").view).toBe("running")
    expect(query("view=bogus").view).toBe("needs_you")
  })

  it("leaves the order to the view until a sort is chosen", () => {
    expect(query("view=done")).not.toHaveProperty("order")
    expect(query("sort=oldest")).toMatchObject({ sort: "updated_at", order: "asc" })
    expect(query("sort=newest")).toMatchObject({ sort: "updated_at", order: "desc" })
    expect(query("sort=amount")).toMatchObject({ sort: "amount", order: "desc" })
    expect(query("sort=amount-asc")).toMatchObject({ sort: "amount", order: "asc" })
  })

  it("sends the search and every facet value", () => {
    expect(
      query("q=%205100%20&exception=price_variance&exception=duplicate&pack=a&pack=b")
    ).toMatchObject({
      q: "5100",
      exceptionTypes: ["price_variance", "duplicate"],
      packIds: ["a", "b"],
    })
    expect(query("q=%20")).not.toHaveProperty("q")
  })
})
