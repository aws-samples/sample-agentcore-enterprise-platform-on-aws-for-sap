// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest"
// @ts-expect-error: a plain ES module the ESLint config loads; it has no types
import { findBanned } from "../../eslint/console-rules.mjs"

/**
 * The class matcher behind `console/banned-classes`. A pattern that matched too little
 * would let a page restyle itself silently; one that matched too much would bury the real
 * warnings, so each case lists exactly the tokens it must flag.
 */
const CASES: Record<string, string[]> = {
  "space-y-2 gap-2": ["space-y-2"],
  "dark:bg-card hover:dark:text-foreground": ["dark:bg-card", "hover:dark:text-foreground"],
  "w-[var(--dock-w)] h-[1px] [writing-mode:vertical-rl] grid-cols-2": [
    "w-[var(--dock-w)]",
    "h-[1px]",
    "[writing-mode:vertical-rl]",
  ],
  "text-2xs text-xs text-3xs": ["text-2xs", "text-3xs"],
  "h-7 h-8 h-10": ["h-7", "h-10"],
  "rounded rounded-md rounded-lg rounded-full rounded-sm rounded-t-md rounded-t-sm rounded-xl": [
    "rounded",
    "rounded-sm",
    "rounded-t-sm",
    "rounded-xl",
  ],
  "z-10 shadow-sm shadow": ["z-10", "shadow-sm", "shadow"],
  "bg-foreground/30 text-muted-foreground": ["bg-foreground/30"],
  "bg-red-500 text-white bg-primary hover:bg-zinc-100": ["bg-red-500", "text-white", "hover:bg-zinc-100"],
  "[&>svg]:size-4 md:flex": [],
}

describe("console/banned-classes", () => {
  for (const [classes, flagged] of Object.entries(CASES)) {
    it(`flags exactly ${JSON.stringify(flagged)} in "${classes}"`, () => {
      expect(findBanned(classes).map((h: { token: string }) => h.token)).toEqual(flagged)
    })
  }
})
