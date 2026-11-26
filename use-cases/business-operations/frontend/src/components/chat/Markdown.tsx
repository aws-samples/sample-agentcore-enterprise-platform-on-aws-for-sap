// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react"
import { Button } from "@/components/ui/button"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Copy, Check } from "lucide-react"

function completePartialMarkdown(text: string): string {
  const fenceCount = (text.match(/^```/gm) || []).length
  if (fenceCount % 2 !== 0) return text + "\n```"
  return text
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = () => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <Button variant="ghost" size="icon-xs" onClick={handleCopy} aria-label="Copy code">
      {copied ? <Check /> : <Copy />}
    </Button>
  )
}

// react-markdown v10 + React 19 has overly strict component types for element-specific refs.
// Using Record<string, ...> to avoid the type mismatch on pre, p, th, td, etc.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const components: Record<string, any> = {
  code({ className, children }: { className?: string; children?: React.ReactNode }) {
    const match = /language-(\w+)/.exec(className || "")
    const codeString = String(children).replace(/\n$/, "")
    if (match) {
      return (
        <div className="my-2 rounded-md overflow-hidden border border-border bg-card">
          <div className="flex items-center justify-between px-3 py-1 bg-muted border-b border-border">
            <span className="text-xs text-muted-foreground">{match[1]}</span>
            <CopyButton text={codeString} />
          </div>
          {/* No highlighting: SOP and ABAP snippets read fine as plain mono, and the
              highlighter was a tenth of the bundle. */}
          <pre className="overflow-x-auto p-3 font-mono text-xs">
            <code>{codeString}</code>
          </pre>
        </div>
      )
    }
    return (
      // `text-xs`: mono matched to the prose by size reads larger than the prose around
      // it, the wrong way round for something quoted mid-sentence.
      <code className="rounded-md bg-muted px-1 py-0.5 font-mono text-xs">{children}</code>
    )
  },
  pre({ children }: { children?: React.ReactNode }) {
    return <>{children}</>
  },
  // Prose links live here rather than in a global `a` rule, which used to leak
  // onto every router <Link>. External hrefs open in a new tab with noreferrer:
  // this content is model-generated, so the URL is untrusted input.
  a({ href, children }: { href?: string; children?: React.ReactNode }) {
    const external = /^https?:\/\//i.test(href ?? "")
    return (
      <a
        href={href}
        {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
        className="text-link underline decoration-1 underline-offset-2 hover:no-underline"
      >
        {children}
      </a>
    )
  },
}

/** The markdown stack itself. Import `MarkdownRenderer`, which loads this on demand. */
export default function Markdown({ content }: { content: string }) {
  return (
    <div className="markdown-body leading-relaxed [&_p]:my-1.5 [&_ul]:my-1.5 [&_ul]:pl-5 [&_ul]:list-disc [&_ol]:my-1.5 [&_ol]:pl-5 [&_ol]:list-decimal [&_li]:my-0.5 [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:mt-3 [&_h1]:mb-1.5 [&_h2]:text-base [&_h2]:font-semibold [&_h2]:mt-2.5 [&_h2]:mb-1 [&_h3]:text-sm [&_h3]:font-semibold [&_h3]:mt-2 [&_h3]:mb-1 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:my-1.5 [&_blockquote]:text-foreground [&_table]:my-2 [&_table]:min-w-full [&_table]:border-collapse [&_table]:text-xs [&_th]:px-2 [&_th]:py-1 [&_th]:bg-muted [&_th]:border [&_th]:border-border [&_th]:text-left [&_th]:font-medium [&_td]:px-2 [&_td]:py-1 [&_td]:border [&_td]:border-border [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {completePartialMarkdown(content)}
      </ReactMarkdown>
    </div>
  )
}
