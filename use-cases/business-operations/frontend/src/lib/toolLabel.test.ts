// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { bareToolName, toolTick } from "@/lib/toolLabel"

describe("bareToolName", () => {
  it("strips the Gateway target prefix", () => {
    expect(bareToolName("erp-gw___odata_read")).toBe("odata_read")
    expect(bareToolName("odata_read")).toBe("odata_read")
    expect(bareToolName("")).toBe("")
  })
})

describe("toolTick", () => {
  it("names the entity a read acted on", () => {
    const tick = toolTick(
      "sap___odata_read",
      '{"service_name":"API_SUPPLIERINVOICE","entity_set":"A_SupplierInvoice"}',
      "complete"
    )
    // entity_set outranks service_name: the entity is what the operator recognises.
    expect(tick).toEqual({ verb: "read", subject: "A_SupplierInvoice", writes: false })
  })

  it("takes the document number out of an OData filter", () => {
    const tick = toolTick(
      "odata_read",
      `{"entity_set":"A_SupplierInvoice","filter":"SupplierInvoice eq '5100001976' and FiscalYear eq '2026'"}`,
      "complete"
    )
    expect(tick.subject).toBe("A_SupplierInvoice 5100001976/2026")
  })

  it("takes the key out of identifier_fields on a write", () => {
    const tick = toolTick(
      "odata_update",
      '{"entity_set":"A_PurchaseOrderItem","identifier_fields":{"PurchaseOrder":"4500000123","PurchaseOrderItem":"10"},"payload":{"NetPriceAmount":"12.50"}}',
      "complete"
    )
    expect(tick).toEqual({
      verb: "updated",
      subject: "A_PurchaseOrderItem 4500000123/10",
      writes: true,
    })
  })

  it("marks only the calls that change SAP as writes", () => {
    // A case-state write and a ticket are not ERP writes, and the spine must not
    // give them the weight of one.
    expect(toolTick("update_case_state", '{"case_id":"A-1"}', "complete").writes).toBe(false)
    expect(toolTick("demo_create_ticket", '{"title":"Review"}', "complete").writes).toBe(false)
    expect(toolTick("odata_function_import", '{"function_name":"Post"}', "complete").writes).toBe(
      true
    )
  })

  it("reads partial streamed arguments rather than waiting for valid JSON", () => {
    // A live call's arguments arrive token by token, so the tick has to name its
    // object off a blob that is not yet parseable.
    const tick = toolTick("odata_read", '{"entity_set":"A_SupplierInvoice","filt', "streaming")
    expect(tick).toEqual({ verb: "reading", subject: "A_SupplierInvoice", writes: false })
  })

  it("reads a stored tool_input that was truncated mid-string", () => {
    // `evidence.py` cuts tool_input at 256 bytes after extracting evidence, so a
    // replayed tick — the common case — routinely gets invalid JSON.
    const tick = toolTick("odata_update", '{"entity_set":"A_SupplierInvoice","payl', "complete")
    expect(tick.subject).toBe("A_SupplierInvoice")
  })

  it("uses the past tense only for a call that completed", () => {
    const args = '{"entity_set":"A_SupplierInvoice"}'
    expect(toolTick("odata_update", args, "executing").verb).toBe("updating")
    // `incomplete` means the run ended without a result — the write may have landed,
    // so claiming "updated" would report an outcome nobody confirmed. `error` reads as
    // the attempt too, with the row's own banner saying how it ended.
    expect(toolTick("odata_update", args, "incomplete").verb).toBe("updating")
    expect(toolTick("odata_update", args, "error").verb).toBe("updating")
    expect(toolTick("odata_update", args, "complete").verb).toBe("updated")
  })

  it("names an unknown tool rather than inventing a verb for it", () => {
    expect(toolTick("sap___some_new_tool", "{}", "complete")).toEqual({
      verb: "ran",
      subject: "some_new_tool",
      writes: false,
    })
    expect(toolTick("some_new_tool", '{"case_id":"A-1"}', "executing")).toEqual({
      verb: "running",
      subject: "some_new_tool A-1",
      writes: false,
    })
  })

  it("still says what it is doing when the arguments name nothing", () => {
    expect(toolTick("odata_read", "", "executing")).toEqual({
      verb: "reading",
      subject: undefined,
      writes: false,
    })
  })

  it("keeps the subject to one line", () => {
    const tick = toolTick("search_sap_api_docs", `{"query":"${"x".repeat(200)}"}`, "complete")
    expect(tick.subject).toHaveLength(56)
    expect(tick.subject?.endsWith("…")).toBe(true)
  })
})
