// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi } from "vitest"
import { renderHook, waitFor } from "@testing-library/react"

const config: { brand?: { name: string; subtitle: string; monogram?: string } } = {}
vi.mock("@/lib/config", () => ({ getConfig: vi.fn(async () => config) }))

import { monogramOf, useBrand } from "@/hooks/useBrand"
import { BRAND } from "@/brand"

describe("useBrand", () => {
  it("falls back to brand.ts when the config carries no brand", async () => {
    const { result } = renderHook(() => useBrand())
    await waitFor(() => expect(document.title).toBe(BRAND.name))
    expect(result.current).toEqual(BRAND)
  })

  it("takes the name and subtitle from console.brand, with a monogram from the name", async () => {
    config.brand = { name: "Accounts Desk", subtitle: "Supplier invoices" }
    const { result } = renderHook(() => useBrand())
    await waitFor(() => expect(result.current.name).toBe("Accounts Desk"))
    expect(result.current.subtitle).toBe("Supplier invoices")
    expect(result.current.monogram).toBe("AD")
    expect(document.title).toBe("Accounts Desk")
  })

  it("takes console.brand.monogram when set, and only letters or digits", async () => {
    config.brand = { name: "Accounts Desk", subtitle: "Supplier invoices", monogram: "AP3" }
    const first = renderHook(() => useBrand())
    await waitFor(() => expect(first.result.current.monogram).toBe("AP3"))
    config.brand = { name: "Accounts Desk", subtitle: "x", monogram: "<b>" }
    const second = renderHook(() => useBrand())
    await waitFor(() => expect(second.result.current.name).toBe("Accounts Desk"))
    expect(second.result.current.monogram).toBe("AD")
    config.brand = { name: BRAND.name, subtitle: "x", monogram: "ABCD" }
    const third = renderHook(() => useBrand())
    await waitFor(() => expect(third.result.current.subtitle).toBe("x"))
    expect(third.result.current.monogram).toBe(BRAND.monogram)
  })

  it("makes a monogram from one word too", () => {
    expect(monogramOf("Finance")).toBe("FI")
  })
})
