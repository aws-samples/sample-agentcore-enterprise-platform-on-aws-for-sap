// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A stored tool payload, made readable — or nothing, if it was never JSON.
 *
 * Two things are wrong with these blobs as they arrive. They are minified, and
 * `agent/utils/evidence.py` cuts `tool_result` at 512 bytes and
 * `tool_input` at 256 on a character boundary, which lands mid-string far more
 * often than not. A cut fragment does not parse, so the indent-if-you-can that
 * this replaced fell through to the raw text — which is why the
 * Result pane was a 512-character wall while Input beside it printed cleanly.
 *
 * Closing the fragment's own open string and brackets is enough to print its
 * structure, and it invents nothing: every key and value shown is one the
 * payload actually carries. The caller is told when that happened, because an
 * audit preview that silently looks complete is worse than an ugly one.
 */
export interface PrettyJson {
  text: string
  /** The payload was cut mid-token and closed to print. Say so on screen. */
  repaired: boolean
}

/**
 * Pretty-print `raw`, repairing a truncated fragment if that is what it is.
 * Returns null when the text is not JSON at all — prose, an error message, a
 * bare SAP string — so a caller can render it as the plain text it is.
 */
export function prettyJson(raw: string): PrettyJson | null {
  const trimmed = raw.trim()
  // Only an object or an array is worth indenting. A quoted scalar gains nothing.
  if (!/^[[{]/.test(trimmed)) return null
  try {
    return { text: JSON.stringify(JSON.parse(trimmed), null, 2), repaired: false }
  } catch {
    const closed = repair(trimmed)
    return closed ? { text: JSON.stringify(closed.value, null, 2), repaired: true } : null
  }
}

/**
 * Close what the cut left open, backing off a value at a time until it parses.
 *
 * One pass records the bracket stack at every top-level-or-nested comma. The
 * whole fragment is tried first — enough when the cut landed inside a string or
 * between values. Failing that, each comma from the last backwards is a place
 * where a complete value ended, so slicing there and closing the brackets that
 * were open at that moment yields valid JSON with only the half-read entry lost.
 */
function repair(fragment: string): { value: unknown } | null {
  const cuts: number[] = []
  const stacks: string[][] = []
  const stack: string[] = []
  let inString = false
  let escaped = false

  for (let i = 0; i < fragment.length; i++) {
    const c = fragment[i]
    if (inString) {
      if (escaped) escaped = false
      else if (c === "\\") escaped = true
      else if (c === '"') inString = false
      continue
    }
    if (c === '"') inString = true
    else if (c === "{") stack.push("}")
    else if (c === "[") stack.push("]")
    else if (c === "}" || c === "]") stack.pop()
    else if (c === ",") {
      cuts.push(i)
      stacks.push([...stack])
    }
  }

  const close = (open: string[]) => [...open].reverse().join("")
  // A trailing backslash would escape the quote we are about to add and leave the
  // string open, so the cut takes it with it.
  const head = escaped ? fragment.slice(0, -1) : fragment
  const candidates = [(inString ? head + '"' : head) + close(stack)]
  for (let k = cuts.length - 1; k >= 0; k--) {
    candidates.push(fragment.slice(0, cuts[k]) + close(stacks[k]))
  }

  for (const candidate of candidates) {
    try {
      return { value: JSON.parse(candidate) }
    } catch {
      // Next candidate: this one still ends on a key with no value, or worse.
    }
  }
  return null
}
