// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react"

/**
 * The editor template (plan, Page templates): the document at its reading measure and a
 * 288 px side panel for the values it cites and the check, stacked below 960 px. A container
 * query, so opening the assistant reflows it.
 */
export function EditorLayout({ main, side }: { main: ReactNode; side: ReactNode }) {
  return (
    <div className="@container">
      <div className="grid items-start gap-8 @[60rem]:grid-cols-[minmax(0,1fr)_18rem]">
        <div className="min-w-0">{main}</div>
        <aside className="flex flex-col gap-4 @[60rem]:sticky @[60rem]:top-0">{side}</aside>
      </div>
    </div>
  )
}
