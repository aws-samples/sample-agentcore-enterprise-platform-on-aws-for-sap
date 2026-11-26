// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { CaseRating, ratingScore } from "@/components/case/CaseRating"
import { submitCaseRating } from "@/services/casesService"
import { UserRating } from "@/types/cases"

vi.mock("@/services/casesService", () => ({ submitCaseRating: vi.fn() }))

const ratings = {
  "sub-me": { rating: UserRating.Negative, name: "me@example.com", at: "2026-11-02T10:00:00Z" },
  "sub-a": { rating: UserRating.Positive, name: "a@example.com", at: "2026-11-02T11:00:00Z" },
  "sub-b": { rating: UserRating.Positive, name: "b@example.com", at: "2026-11-02T09:00:00Z" },
}

describe("CaseRating", () => {
  it("scores positive raters minus negative ones", () => {
    expect(ratingScore(undefined)).toBe(0)
    expect(ratingScore(ratings)).toBe(1)
  })

  it("names every rater and marks the operator's own vote", () => {
    render(
      <CaseRating caseId="ap-1" ratings={ratings} mySub="sub-me" token="t" onSaved={() => {}} />
    )
    expect(screen.getByText("+1 from 3")).toBeTruthy()
    expect(screen.getByText(/a@example\.com/)).toBeTruthy()
    expect(screen.getByLabelText("Bad precedent").getAttribute("aria-pressed")).toBe("true")
    expect(screen.getByLabelText("Good precedent").getAttribute("aria-pressed")).toBe("false")
  })

  it("submits a vote and refreshes the case", async () => {
    const onSaved = vi.fn()
    render(<CaseRating caseId="ap-1" token="t" onSaved={onSaved} />)
    expect(screen.getByText("Not rated")).toBeTruthy()
    fireEvent.click(screen.getByLabelText("Good precedent"))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(submitCaseRating).toHaveBeenCalledWith("ap-1", "positive", undefined, "t")
  })
})
