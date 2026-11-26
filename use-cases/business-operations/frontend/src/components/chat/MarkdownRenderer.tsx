// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Suspense, lazy } from "react"

// The markdown stack is the largest thing in the bundle and the dock renders on every
// route, so it loads on demand. Until it arrives the text shows as written.
const Markdown = lazy(() => import("./Markdown"))

export function MarkdownRenderer({ content }: { content: string }) {
  if (!content) return null
  return (
    <Suspense fallback={<div className="leading-relaxed whitespace-pre-wrap">{content}</div>}>
      <Markdown content={content} />
    </Suspense>
  )
}
