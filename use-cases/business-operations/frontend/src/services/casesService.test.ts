// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest"
import {
  caseQueryParams,
  fetchCaseSummary,
  fetchViewCounts,
  queryCasePages,
  queryCases,
  MAX_CASE_PAGES,
  fetchCase,
  enqueueCases,
  submitCaseRating,
  replyToCase,
  withCaseRecord,
} from "@/services/casesService"
import { CaseStatus, type WorkItem } from "@/types/cases"

vi.mock("@/lib/config", () => ({
  getConfig: vi.fn().mockResolvedValue({
    apiUrl: "https://api.test",
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

describe("casesService", () => {
  it("queryCases sends the view, search, facets and sort the route takes", async () => {
    const fetchMock = okJson({ items: [{ case_id: "a", updated_at: "1" }], next_token: "t" })
    vi.stubGlobal("fetch", fetchMock)

    const page = await queryCases(
      {
        view: "needs_you",
        q: " 5100 ",
        exceptionTypes: ["price_variance", "duplicate"],
        packIds: ["finance_ops"],
        sort: "updated_at",
        order: "asc",
        limit: 50,
      },
      "tok"
    )

    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe(
      "https://api.test/cases?view=needs_you&q=5100&exception_type=price_variance&exception_type=duplicate&pack_id=finance_ops&sort=updated_at&order=asc&limit=50"
    )
    expect(opts.headers.Authorization).toBe("Bearer tok")
    expect(page.next_token).toBe("t")
  })

  it("caseQueryParams sends nothing it was not given, and the token as given", () => {
    expect(caseQueryParams({}).toString()).toBe("")
    expect(caseQueryParams({ view: "all" }, "abc=").toString()).toBe("view=all&next_token=abc%3D")
  })

  it("queryCasePages follows next_token until told to stop, or the page cap", async () => {
    const pages = [
      { items: [{ case_id: "a", updated_at: "1" }], next_token: "t1" },
      { items: [], next_token: "t2" },
      { items: [{ case_id: "b", updated_at: "2" }], next_token: null },
    ]
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => ({
        ok: true,
        status: 200,
        json: async () => pages.shift(),
      }))
    )
    const all = await queryCasePages({ view: "done" }, "tok")
    expect(all.items.map(c => c.case_id)).toEqual(["a", "b"])
    expect(all.more).toBe(false)

    const forever = okJson({ items: [{ case_id: "x", updated_at: "1" }], next_token: "again" })
    vi.stubGlobal("fetch", forever)
    const capped = await queryCasePages({}, "tok")
    expect(forever).toHaveBeenCalledTimes(MAX_CASE_PAGES)
    expect(capped.more).toBe(true)
  })

  it("queryCases says what the route refused", async () => {
    vi.stubGlobal(
      "fetch",
      errResponse(400, { error: "next_token is not this request's", field: "next_token" })
    )
    await expect(queryCases({}, "tok")).rejects.toThrow(/next_token is not this request's/)
  })

  it("fetchCaseSummary asks for a window and the packs", async () => {
    const fetchMock = okJson({ totals: { closed: 2 }, days: [] })
    vi.stubGlobal("fetch", fetchMock)
    await fetchCaseSummary(new Date("2026-11-19T09:00:00Z"), ["finance_ops"], "tok")
    expect(fetchMock.mock.calls[0][0]).toBe(
      "https://api.test/cases/summary?since=2026-11-19T09%3A00%3A00.000Z&pack_id=finance_ops"
    )
  })

  it("fetchViewCounts takes the search and facets, not the view", async () => {
    const fetchMock = okJson({ views: { needs_you: 4 }, counts: {} })
    vi.stubGlobal("fetch", fetchMock)
    const counts = await fetchViewCounts({ q: "51", packIds: ["finance_ops"] }, "tok")
    expect(fetchMock.mock.calls[0][0]).toBe(
      "https://api.test/cases/counts?q=51&pack_id=finance_ops"
    )
    expect(counts.views.needs_you).toBe(4)
  })

  it("fetchCase hits the case_id path", async () => {
    const fetchMock = okJson({ id: "x" })
    vi.stubGlobal("fetch", fetchMock)
    await fetchCase("DOC1-ITEM1", "tok")
    expect(fetchMock.mock.calls[0][0]).toBe("https://api.test/cases/DOC1-ITEM1")
  })

  it("enqueueCases POSTs one request per id with case_id body", async () => {
    const fetchMock = okJson({})
    vi.stubGlobal("fetch", fetchMock)
    await enqueueCases(["a", "b"], "tok")
    expect(fetchMock).toHaveBeenCalledTimes(2)
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.test/cases/enqueue")
    expect(opts.method).toBe("POST")
    expect(JSON.parse(opts.body)).toEqual({ case_id: "a" })
  })

  it("enqueueCases surfaces error body text on failure", async () => {
    vi.stubGlobal("fetch", errResponse(409, "duplicate"))
    await expect(enqueueCases(["a"], "tok")).rejects.toThrow(/duplicate/)
  })

  it("submitCaseRating PUTs rating and comment", async () => {
    const fetchMock = okJson({})
    vi.stubGlobal("fetch", fetchMock)
    await submitCaseRating("DOC1-ITEM1", "positive", "nice", "tok")
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.test/cases/DOC1-ITEM1/rating")
    expect(opts.method).toBe("PUT")
    expect(JSON.parse(opts.body)).toEqual({ rating: "positive", comment: "nice" })
  })

  it("replyToCase POSTs the message and the decision, and never a sender", async () => {
    const fetchMock = okJson({ enqueued: true, case_id: "DOC1-ITEM1" })
    vi.stubGlobal("fetch", fetchMock)
    const result = await replyToCase("DOC1-ITEM1", "Within tolerance", "approved", "tok")
    const [url, opts] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.test/cases/DOC1-ITEM1/reply")
    expect(opts.method).toBe("POST")
    // Exactly two keys: the backend takes the sender from the token, and a sender in the
    // body could only ever be ignored or believed.
    expect(JSON.parse(opts.body)).toEqual({ message: "Within tolerance", decision: "approved" })
    expect(result).toEqual({ enqueued: true, case_id: "DOC1-ITEM1" })
  })

  it("replyToCase surfaces the refusal's cause, not its status", async () => {
    // "403" alone reads as "you cannot touch this case", which is wrong: approval is gated
    // on the domain's approve_groups while a denial needs only read entitlement.
    vi.stubGlobal("fetch", errResponse(403, { error: "Not entitled to this domain" }))
    await expect(replyToCase("DOC1-ITEM1", "ok", "approved", "tok")).rejects.toThrow(
      /Not entitled to this domain/
    )
  })

  it("replyToCase falls back to the status when the refusal carries no cause", async () => {
    vi.stubGlobal("fetch", errResponse(502, "<html>gateway</html>"))
    await expect(replyToCase("DOC1-ITEM1", "ok", "replied", "tok")).rejects.toThrow(
      /Failed to send reply: 502/
    )
  })
})

describe("withCaseRecord", () => {
  const row = (id: string, status: WorkItem["status"], at: string) =>
    ({ case_id: id, status, updated_at: at }) as WorkItem

  it("takes the record's status when the record is newer than the row", () => {
    const list = [
      row("a", CaseStatus.Detected, "2026-11-10T00:00:00Z"),
      row("b", CaseStatus.Detected, "x"),
    ]
    const next = withCaseRecord(list, row("a", CaseStatus.Complete, "2026-11-10T00:01:00Z"))
    expect(next?.[0].status).toBe(CaseStatus.Complete)
    expect(next?.[1]).toBe(list[1])
  })

  it("leaves the list untouched for an older or unknown record", () => {
    const list = [row("a", CaseStatus.Complete, "2026-11-10T00:02:00Z")]
    expect(withCaseRecord(list, row("a", CaseStatus.Detected, "2026-11-10T00:01:00Z"))).toBe(list)
    expect(withCaseRecord(list, row("z", CaseStatus.Complete, "2026-11-10T00:09:00Z"))).toBe(list)
    expect(withCaseRecord(undefined, row("a", CaseStatus.Complete, "x"))).toBeUndefined()
  })
})
