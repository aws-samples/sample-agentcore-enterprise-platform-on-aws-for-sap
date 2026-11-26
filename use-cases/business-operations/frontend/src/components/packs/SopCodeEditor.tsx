// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef } from "react"
import { EditorState } from "@codemirror/state"
import {
  EditorView,
  drawSelection,
  highlightActiveLine,
  keymap,
  lineNumbers,
} from "@codemirror/view"
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands"
import {
  autocompletion,
  completionKeymap,
  type CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete"

/**
 * The SOP editor: CodeMirror with line numbers, undo, and `{{` completing the pack's rules
 * and contact roles with their values. Loaded only when a person presses Edit, so the read
 * view never pays for it (the route imports it with `React.lazy`).
 */
export interface Symbol {
  name: string
  detail: string
}

const theme = EditorView.theme({
  "&": {
    fontSize: "0.875rem",
    backgroundColor: "var(--card)",
    color: "var(--foreground)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
  },
  "&.cm-focused": { outline: "2px solid var(--ring)", outlineOffset: "1px" },
  ".cm-content": {
    fontFamily: "var(--font-mono)",
    padding: "0.5rem 0",
    caretColor: "var(--foreground)",
  },
  ".cm-gutters": {
    backgroundColor: "var(--card)",
    color: "var(--muted-foreground)",
    borderRight: "1px solid var(--border)",
    borderRadius: "var(--radius) 0 0 var(--radius)",
  },
  ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: "var(--muted)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    backgroundColor: "var(--accent)",
  },
  ".cm-tooltip": {
    backgroundColor: "var(--popover)",
    color: "var(--popover-foreground)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
  },
  ".cm-tooltip-autocomplete ul li[aria-selected]": {
    backgroundColor: "var(--accent)",
    color: "var(--accent-foreground)",
  },
  ".cm-completionDetail": { color: "var(--muted-foreground)", marginLeft: "0.75rem" },
})

export default function SopCodeEditor({
  value,
  onChange,
  symbols,
  label,
}: {
  value: string
  onChange: (next: string) => void
  symbols: Symbol[]
  label: string
}) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<EditorView | null>(null)
  const latest = useRef({ onChange, symbols })
  latest.current = { onChange, symbols }

  useEffect(() => {
    const complete = (ctx: CompletionContext): CompletionResult | null => {
      const word = ctx.matchBefore(/\{\{[A-Z0-9_]*/)
      if (!word) return null
      return {
        from: word.from,
        options: latest.current.symbols.map(s => ({
          label: `{{${s.name}}}`,
          detail: s.detail,
          type: "variable",
        })),
        validFor: /^\{\{[A-Z0-9_]*$/,
      }
    }
    const v = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          history(),
          drawSelection(),
          highlightActiveLine(),
          EditorView.lineWrapping,
          autocompletion({ override: [complete], activateOnTyping: true }),
          keymap.of([...completionKeymap, ...defaultKeymap, ...historyKeymap]),
          EditorView.contentAttributes.of({ "aria-label": label }),
          EditorView.updateListener.of(u => {
            if (u.docChanged) latest.current.onChange(u.state.doc.toString())
          }),
          theme,
        ],
      }),
    })
    view.current = v
    return () => v.destroy()
    // Created once; later values are pushed in below without rebuilding the editor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // A value set from outside (Discard) replaces the document.
  useEffect(() => {
    const v = view.current
    if (v && v.state.doc.toString() !== value)
      v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value } })
  }, [value])

  return <div ref={host} className="min-h-0" />
}
