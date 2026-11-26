// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest"
import {
  fetchTickets,
  fetchTicket,
  createTicket,
  updateTicket,
  submitTicketAction,
} from "@/services/ticketsService"
import { TicketStatus } from "@/types/tickets"

vi.mock("@/lib/config", () => ({
  getConfig: vi.fn().mockResolvedValue({
    // Tickets are on the console API.
    apiUrl: "https://api.test",
    ticketingEnabled: true,
  }),
}))

function okJson(body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  })
}

function errResponse(status: number, body: unknown = {}) {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("ticketsService", () => {
  it("fetchTickets omits 'all' status, includes assigned_to", async () => {
    const fetchMock = okJson({ items: [], next_token: null })
    vi.stubGlobal("fetch", fetchMock)
    await fetchTickets({ status: "all" as never, assigned_to: "u1" }, "tok")
    expect(fetchMock.mock.calls[0][0]).toBe("https://api.test/tickets?assigned_to=u1&limit=500")
  })

  it("fetchTickets throws on non-ok", async () => {
    vi.stubGlobal("fetch", errResponse(503))
    await expect(fetchTickets({}, "tok")).rejects.toThrow(/Failed to fetch tickets: 503/)
  })

  it("createTicket POSTs body", async () => {
    const fetchMock = okJson({ id: "T1" })
    vi.stubGlobal("fetch", fetchMock)
    await createTicket({ title: "t", description: "d" }, "tok")
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.test/tickets")
    expect(opts.method).toBe("POST")
    expect(JSON.parse(opts.body)).toEqual({ title: "t", description: "d" })
  })

  it("updateTicket PUTs to the ticket path", async () => {
    const fetchMock = okJson({ id: "T1" })
    vi.stubGlobal("fetch", fetchMock)
    await updateTicket("T1", { status: "closed" }, "tok")
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.test/tickets/T1")
    expect(opts.method).toBe("PUT")
  })

  it("submitTicketAction builds reply comment when action is replied", async () => {
    const fetchMock = okJson({ ticket: {}, enqueued: true, case_id: "c1" })
    vi.stubGlobal("fetch", fetchMock)
    await submitTicketAction("T1", "replied", "see notes", "tok", "extra")
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.comment).toBe("see notes")
    expect(body.response_text).toBe("extra")
  })

  it("submitTicketAction sends the reviewer's own words as the comment on approval", async () => {
    // The reviewer's reason reaches both the comment list and the resumed agent —
    // `resolution` is what the SQS payload carries, so a canned comment would diverge.
    const fetchMock = okJson({ ticket: {}, enqueued: false, case_id: "c1" })
    vi.stubGlobal("fetch", fetchMock)
    await submitTicketAction("T1", "approved", "Within the 5% tolerance", "tok")
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.comment).toBe("Within the 5% tolerance")
    expect(body.resolution).toBe("Within the 5% tolerance")
    expect("response_text" in body).toBe(false)
  })

  it("fetchTicket hits the ticket path", async () => {
    const fetchMock = okJson({ id: "T1" })
    vi.stubGlobal("fetch", fetchMock)
    await fetchTicket("T1", "tok")
    expect(fetchMock.mock.calls[0][0]).toBe("https://api.test/tickets/T1")
  })

  it("fetchTickets with no filter asks only for a page size", async () => {
    const fetchMock = okJson({ items: [], next_token: null })
    vi.stubGlobal("fetch", fetchMock)
    await fetchTickets({}, "tok")
    expect(fetchMock.mock.calls[0][0]).toBe("https://api.test/tickets?limit=500")
  })

  it("fetchTickets follows next_token to the last page and orders newest first", async () => {
    const page = (items: unknown[], next_token: string | null) => ({
      ok: true,
      status: 200,
      json: async () => ({ items, next_token }),
      text: async () => "",
    })
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(page([{ ticket_id: "A", updated_at: "2026-01-01" }], "tok2"))
      .mockResolvedValueOnce(page([{ ticket_id: "B", updated_at: "2026-02-01" }], null))
    vi.stubGlobal("fetch", fetchMock)
    const tickets = await fetchTickets({ status: TicketStatus.Open }, "tok")
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1][0]).toBe(
      "https://api.test/tickets?status=open&limit=500&next_token=tok2"
    )
    expect(tickets.map(t => t.ticket_id)).toEqual(["B", "A"])
  })
})
