// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The two pure halves of the reference client: its flag handling and its printer.
 *
 * Lives here rather than beside the example so it is collected by the same vitest glob as
 * the rest of the package, and so importing it typechecks `examples/follow-run.ts` — which
 * is otherwise reachable from nothing and would rot quietly. The transport halves
 * (`fetch`, the socket) are the ones the example exists to demonstrate and are covered by
 * `channel.test.ts` and `runFollower.test.ts` respectively.
 */

import { describe, expect, it } from "vitest"
import { parseArgs, renderFrame, type RenderState } from "../../examples/follow-run.ts"
import type { AppliedFrame } from "../runFollower.ts"
import type { Message, ToolCallStatus } from "../types.ts"

const REQUIRED = {
  ERP_API_URL: "https://api.example.test/prod",
  ERP_EVENTS_HTTP_DOMAIN: "events.example.test",
  ERP_EVENTS_REALTIME_DOMAIN: "realtime.example.test",
  ERP_ID_TOKEN: "id-token",
}

function state(): RenderState {
  return { text: new Map(), tools: new Set() }
}

function frame(turn: Message[]): AppliedFrame {
  return { slot: "5100001976-2026", runId: "run-8f1c", event: {}, turn, terminal: false }
}

function assistant(id: string, content: string, segments?: Message["segments"]): Message {
  return { id, role: "assistant", content, timestamp: "2026-08-27T12:00:00.000Z", segments }
}

function tool(toolUseId: string, name: string, status: ToolCallStatus): Message["segments"] {
  return [{ type: "tool", toolCall: { toolUseId, name, input: "{}", status } }]
}

describe("parseArgs", () => {
  it("reads every required value from the environment", () => {
    const parsed = parseArgs(["hello", "there"], REQUIRED)
    expect(parsed).toEqual({
      ok: true,
      options: {
        api: "https://api.example.test/prod",
        httpDomain: "events.example.test",
        realtimeDomain: "realtime.example.test",
        token: "id-token",
        caseId: "",
        // A question unless asked to process: the route's default writes nothing.
        process: false,
        // Joined, so an unquoted prompt on the command line is not silently truncated to
        // its first word.
        prompt: "hello there",
        timeoutMs: 600_000,
      },
    })
  })

  it("prefers a flag over the environment, in either spelling", () => {
    const parsed = parseArgs(
      ["--api=https://flag.example.test/prod/", "--case", "5100001976-2026", "hi"],
      REQUIRED
    )
    expect(parsed.ok && parsed.options.api).toBe("https://flag.example.test/prod")
    expect(parsed.ok && parsed.options.caseId).toBe("5100001976-2026")
  })

  it("processes only a named case, and only when asked to", () => {
    const parsed = parseArgs(["--case", "5100001976-2026", "--process", "go"], REQUIRED)
    expect(parsed.ok && parsed.options.process).toBe(true)
    expect(parsed.ok && parsed.options.prompt).toBe("go")
    const caseless = parseArgs(["--process", "go"], REQUIRED)
    expect(!caseless.ok && caseless.message).toContain("--process needs --case")
  })

  it("names every value it is missing, rather than only the first", () => {
    const parsed = parseArgs([], {})
    expect(parsed.ok).toBe(false)
    expect(!parsed.ok && parsed.help).toBe(false)
    expect(!parsed.ok && parsed.message).toContain("--api, --http-domain")
    expect(!parsed.ok && parsed.message).toContain("<prompt>")
  })

  it("answers --help with the usage text and nothing else", () => {
    // Before the required-value check, so `--help` on its own is help and not an error.
    const parsed = parseArgs(["--help"], {})
    expect(parsed.ok).toBe(false)
    expect(!parsed.ok && parsed.help).toBe(true)
    expect(!parsed.ok && parsed.message).toContain("Usage:")
  })

  it("refuses a flag left without a value", () => {
    const parsed = parseArgs(["hi", "--token"], REQUIRED)
    expect(!parsed.ok && parsed.message).toBe("--token needs a value.")
  })

  it("refuses a timeout that is not a positive number of seconds", () => {
    // Number("soon") is NaN, and a NaN timeout fires immediately — so the run would be
    // abandoned the moment it started, reported as a timeout.
    expect(!parseArgs(["--timeout", "soon", "hi"], REQUIRED).ok).toBe(true)
    expect(!parseArgs(["--timeout", "0", "hi"], REQUIRED).ok).toBe(true)
    expect(parseArgs(["--timeout", "30", "hi"], REQUIRED)).toMatchObject({
      options: { timeoutMs: 30_000 },
    })
  })
})

describe("renderFrame", () => {
  it("prints only what the frame added", () => {
    const seen = state()
    expect(renderFrame(frame([assistant("msg-1", "Hel")]), seen)).toEqual(["Hel"])
    expect(renderFrame(frame([assistant("msg-1", "Hello")]), seen)).toEqual(["lo"])
    // The projection is unchanged — a frame that added no text prints nothing, rather than
    // repeating the message.
    expect(renderFrame(frame([assistant("msg-1", "Hello")]), seen)).toEqual([])
  })

  it("tracks each message separately, so a second one is not sliced by the first", () => {
    const seen = state()
    renderFrame(frame([assistant("msg-1", "A long first message.")]), seen)
    // What a MESSAGES_SNAPSHOT does: the turn now holds two assistant messages, and the
    // second is shorter than the first. A single high-water mark over "the last message"
    // would print nothing here and then mis-slice everything after it.
    expect(
      renderFrame(
        frame([assistant("msg-1", "A long first message."), assistant("msg-2", "Hi")]),
        seen
      )
    ).toEqual(["Hi"])
  })

  it("announces a tool once, and not before its name is settled", () => {
    const seen = state()
    // Still streaming its arguments: the adapter may not have the name yet.
    expect(renderFrame(frame([assistant("m", "", tool("t-1", "", "streaming"))]), seen)).toEqual([])
    expect(
      renderFrame(frame([assistant("m", "", tool("t-1", "sap_read", "executing"))]), seen)
    ).toEqual(["\n[tool] sap_read"])
    // Its result arriving is the same card, not a second one.
    expect(
      renderFrame(frame([assistant("m", "", tool("t-1", "sap_read", "complete"))]), seen)
    ).toEqual([])
  })
})
