// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { Ident } from "@/components/Ident"
import { Fragment, type ReactNode } from "react"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

/**
 * A SOP's plain text, read as a document: sections from the `====` rulers, numbered clauses
 * with their number in the margin, bullets, and the indented formulas kept as written. Every
 * `{{SYMBOL}}` shows the value this deployment resolves it to, with its name on hover. At a
 * 72-character measure, because the clauses are long and read word by word.
 */

export type SopBlock =
  { type: "p"; text: string } | { type: "bullet"; text: string } | { type: "code"; lines: string[] }

export interface SopClause {
  number: string | null
  blocks: SopBlock[]
}

export interface SopSection {
  id: string
  title: string
  clauses: SopClause[]
}

export interface ParsedSop {
  version: string | null
  sections: SopSection[]
}

const RULER = /^={10,}\s*$/
const CLAUSE = /^(\d+(?:\.\d+)*\.?[a-z]?)\s+(\S.*)$/

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")

const sentenceCase = (s: string) =>
  s === s.toUpperCase() ? s.charAt(0) + s.slice(1).toLowerCase() : s

export function parseSop(text: string): ParsedSop {
  const lines = text.replace(/\r\n/g, "\n").split("\n")
  const version = text.match(/^Version\s+(\S+)\s*$/m)?.[1] ?? null
  const sections: SopSection[] = []
  // Held in one object so the helpers below see the current section and clause.
  const at: {
    section: SopSection | null
    clause: SopClause | null
    indent: number
    open: boolean
  } = { section: null, clause: null, indent: 0, open: false }

  const last = () => at.clause?.blocks[at.clause.blocks.length - 1]
  const startClause = (number: string | null): SopClause => {
    const clause: SopClause = { number, blocks: [] }
    at.section!.clauses.push(clause)
    at.clause = clause
    return clause
  }

  for (let k = 0; k < lines.length; k++) {
    const line = lines[k]
    if (RULER.test(line)) {
      // `====` / TITLE / `====` names a section.
      const title = lines[k + 1]?.trim()
      if (title && RULER.test(lines[k + 2] ?? "")) {
        at.section = { id: slug(title), title: sentenceCase(title), clauses: [] }
        sections.push(at.section)
        at.clause = null
        k += 2
      }
      continue
    }
    if (!at.section) continue // the title block above the first section
    if (!line.trim()) {
      at.open = false
      continue
    }
    const m = line.match(CLAUSE)
    if (m) {
      startClause(m[1].replace(/\.$/, "")).blocks.push({ type: "p", text: m[2] })
      at.indent = line.length - m[2].length
      at.open = true
      continue
    }
    const lead = line.length - line.trimStart().length
    if (!at.clause || lead === 0) {
      const clause = !at.clause || at.clause.number ? startClause(null) : at.clause
      const prev = last()
      // Unnumbered text at the margin; aligned key and value pairs keep their columns.
      if (/\S\s{2,}\S/.test(line.trim())) {
        if (prev?.type === "code") prev.lines.push(line)
        else clause.blocks.push({ type: "code", lines: [line] })
      } else if (at.open && prev?.type === "p") prev.text += ` ${line.trim()}`
      else clause.blocks.push({ type: "p", text: line.trim() })
      at.open = true
      continue
    }
    const clause = at.clause
    const rel = line.slice(Math.min(lead, at.indent))
    const relLead = rel.length - rel.trimStart().length
    const prev = last()
    if (rel.startsWith("- ")) clause.blocks.push({ type: "bullet", text: rel.slice(2).trim() })
    else if (relLead === 0 && lead >= at.indent) {
      if (at.open && prev?.type === "p") prev.text += ` ${rel.trim()}`
      else clause.blocks.push({ type: "p", text: rel.trim() })
    } else if (prev?.type === "bullet" && relLead <= 2) prev.text += ` ${rel.trim()}`
    else if (prev?.type === "code") prev.lines.push(rel)
    else clause.blocks.push({ type: "code", lines: [rel] })
    at.open = true
  }
  return { version, sections }
}

export type Resolve = (symbol: string) => { value: string; note?: string } | null

/** Text with `{{SYMBOL}}` resolved, and `code` spans in mono. */
function Inline({ text, resolve }: { text: string; resolve: Resolve }) {
  const parts = text.split(/(\$?\{\{[A-Z][A-Z0-9_]*\}\}|`[^`]+`)/g)
  return (
    <>
      {parts.map((part, i) => {
        const sym = part.match(/^\$?\{\{([A-Z][A-Z0-9_]*)\}\}$/)
        if (sym) {
          const hit = resolve(sym[1])
          return (
            <Tooltip key={i}>
              <TooltipTrigger
                render={<span className="rounded-md bg-muted px-1 font-medium text-foreground" />}
              >
                {hit?.value ?? sym[1]}
              </TooltipTrigger>
              <TooltipContent>
                <span className="font-mono">{sym[1]}</span>
                {hit?.note ? ` · ${hit.note}` : hit ? "" : " · not a value of this pack"}
              </TooltipContent>
            </Tooltip>
          )
        }
        if (part.startsWith("`") && part.endsWith("`") && part.length > 1)
          return <Ident key={i}>{part.slice(1, -1)}</Ident>
        return <Fragment key={i}>{part}</Fragment>
      })}
    </>
  )
}

function Block({ b, resolve }: { b: SopBlock; resolve: Resolve }): ReactNode {
  // Aligned text: key and value pairs become rows whose columns line up, in the prose's own
  // face; anything else keeps its line breaks. Mono stays for the identifiers inside.
  if (b.type === "code") {
    const rows = b.lines.map(l => l.trim().split(/\s{2,}/))
    if (rows.every(r => r.length >= 2))
      return (
        <Table className="w-auto">
          <TableBody>
            {rows.map((r, i) => (
              <TableRow key={i} className="border-0 hover:bg-transparent">
                {r.map((cell, j) => (
                  <TableCell
                    key={j}
                    className={cn(
                      "py-0.5 pr-6 pl-0 align-top whitespace-normal",
                      j === 0 && "text-muted-foreground"
                    )}
                  >
                    <Inline text={cell} resolve={resolve} />
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )
    return (
      <p className="leading-relaxed break-words whitespace-pre-wrap">
        {b.lines.map((l, i) => (
          <Fragment key={i}>
            <Inline text={l} resolve={resolve} />
            {i < b.lines.length - 1 ? "\n" : ""}
          </Fragment>
        ))}
      </p>
    )
  }
  if (b.type === "bullet")
    return (
      <p className="flex gap-2">
        <span aria-hidden="true" className="text-muted-foreground">
          –
        </span>
        <span>
          <Inline text={b.text} resolve={resolve} />
        </span>
      </p>
    )
  return (
    <p>
      <Inline text={b.text} resolve={resolve} />
    </p>
  )
}

export function SopProse({ sop, resolve }: { sop: ParsedSop; resolve: Resolve }) {
  return (
    <article className="flex max-w-measure flex-col gap-8 text-base leading-relaxed">
      {sop.sections.map(s => (
        <section key={s.id} aria-labelledby={`sop-${s.id}`} className="flex flex-col gap-3">
          <h2 id={`sop-${s.id}`} className="scroll-mt-4 text-base font-semibold">
            {s.title}
          </h2>
          {s.clauses.map((c, i) => (
            <div key={i} className="flex gap-3">
              {c.number && (
                <span className="w-10 flex-none pt-px text-sm text-muted-foreground tabular-nums">
                  {c.number}
                </span>
              )}
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                {c.blocks.map((b, j) => (
                  <Block key={j} b={b} resolve={resolve} />
                ))}
              </div>
            </div>
          ))}
        </section>
      ))}
    </article>
  )
}
