// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { beforeAll, describe, it, expect } from "vitest"
import { render, screen } from "@testing-library/react"
import { MarkdownRenderer } from "./MarkdownRenderer"

// The markdown stack loads on demand, so every assertion waits for it.
describe("MarkdownRenderer", () => {
  // The first test after a cold start would otherwise spend findBy's whole timeout on the
  // import. The fallback test runs before this resolves into the lazy cache.
  beforeAll(() => import("./Markdown"))

  it("shows the text as written until the markdown stack loads", () => {
    render(<MarkdownRenderer content="**bold**" />)
    expect(screen.getByText("**bold**")).toBeTruthy()
  })

  it("renders a fenced block as plain code under its language label", async () => {
    render(<MarkdownRenderer content={"```python\nx = 1\n```"} />)
    expect(await screen.findByText("python")).toBeTruthy()
    expect(screen.getByText("x = 1").tagName).toBe("CODE")
  })

  it("opens external prose links in a new tab", async () => {
    render(<MarkdownRenderer content="[docs](https://example.com)" />)
    const link = await screen.findByRole("link", { name: "docs" })
    expect(link.getAttribute("target")).toBe("_blank")
    expect(link.getAttribute("rel")).toBe("noopener noreferrer")
  })

  it("leaves relative links in the same tab", async () => {
    render(<MarkdownRenderer content="[case](/cases/123)" />)
    expect((await screen.findByRole("link", { name: "case" })).getAttribute("target")).toBeNull()
  })

  it("closes an unterminated code fence while streaming", async () => {
    render(<MarkdownRenderer content={"```json\n{"} />)
    expect(await screen.findByText("json")).toBeTruthy()
  })
})
