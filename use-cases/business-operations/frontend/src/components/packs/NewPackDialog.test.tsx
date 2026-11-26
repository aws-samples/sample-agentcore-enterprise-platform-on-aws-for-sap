// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react"

import { NewPackDialog } from "@/components/packs/NewPackDialog"
import type { PackSpec } from "@/services/packsService"

/**
 * The seam this dialog adds: a create is a *spec*, and a human presses Create.
 *
 * So what is pinned is the refusal and the body — that the form will not post a spec the
 * API would reject, and that what it does post carries only what the analyst typed. The
 * privileged half (`gateway_tools`, `model_tier`, `source`, `fields`) is the composer's,
 * and a field here that could widen it would show up as an extra key in these calls.
 */

afterEach(cleanup)

type Create = (packId: string, spec: PackSpec) => Promise<unknown>

function open(onCreate = vi.fn<Create>(async () => ({}))) {
  render(<NewPackDialog open taken={["finance_ops"]} onClose={vi.fn()} onCreate={onCreate} />)
  return onCreate
}

/** Every value the form requires, so a test only has to break the one it is about. */
function fillIn() {
  for (const [label, value] of [
    ["Pack id", "field_service_parts"],
    ["Case id prefix", "fsp"],
    ["Display name", "Field Service Parts"],
    ["Description", "Parts shortages."],
    ["Definition", "A confirmed service order whose parts are not in stock."],
    ["Process types", "parts_shortage"],
    ["Attribute 1 name", "order_number"],
    ["Attribute 1 label", "Service Order"],
  ] as const) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
}

const create = () => screen.getByRole("button", { name: "Create pack" })

describe("NewPackDialog", () => {
  it("refuses a pack id that is already published, and says which", () => {
    open()
    fillIn()
    fireEvent.change(screen.getByLabelText("Pack id"), { target: { value: "finance_ops" } })

    expect(create()).toBeDisabled()
    expect(screen.getByText(/finance_ops is already published here/)).toBeInTheDocument()
  })

  it("shows one problem at a time, and counts the rest", () => {
    open()
    // Nothing typed: the pack id is the first refusal, and there are several more.
    expect(screen.getByText(/Pack id is lowercase letters.*\(\d+ more\)/)).toBeInTheDocument()
    expect(create()).toBeDisabled()
  })

  it("posts what was typed and nothing the schema already implies", async () => {
    const onCreate = open()
    fillIn()
    fireEvent.click(create())

    await waitFor(() => expect(onCreate).toHaveBeenCalled())
    expect(onCreate.mock.calls[0]).toEqual([
      "field_service_parts",
      {
        display_name: "Field Service Parts",
        description: "Parts shortages.",
        short_id: "fsp",
        kind: "filter",
        definition: "A confirmed service order whose parts are not in stock.",
        process_types: ["parts_shortage"],
        // `panel` is absent: the schema's default is true, and no constants were added.
        attributes: [{ name: "order_number", label: "Service Order", type: "string" }],
      },
    ])
  })

  it("carries the rules as numbers", async () => {
    const onCreate = open()
    fillIn()
    fireEvent.click(screen.getByRole("button", { name: "Add rule" }))
    fireEvent.change(screen.getByLabelText("Rule 1 name"), {
      target: { value: "MIN_STOCK_PCT" },
    })
    fireEvent.change(screen.getByLabelText("Rule 1 value"), { target: { value: "80" } })
    fireEvent.click(create())

    await waitFor(() => expect(onCreate).toHaveBeenCalled())
    expect(onCreate.mock.calls[0][1]).toMatchObject({ constants: { MIN_STOCK_PCT: 80 } })
  })

  it("stays open with the reason when the API refuses the create", async () => {
    open(
      vi.fn<Create>(async () => {
        throw new Error("field_service_parts is not yours to author")
      })
    )
    fillIn()
    fireEvent.click(create())

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("not yours to author"))
    // Still the form, still filled in — the analyst fixes it rather than retyping it.
    expect((screen.getByLabelText("Pack id") as HTMLInputElement).value).toBe("field_service_parts")
  })
})
