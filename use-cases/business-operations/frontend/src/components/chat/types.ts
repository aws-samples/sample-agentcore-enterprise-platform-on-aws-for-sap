// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The chat model belongs to the run protocol, not to these components: the reducer that
 * builds a `Message` and the trace replay that seeds one both live in `@business-operations/run-client`,
 * which has no React in it and is what a non-browser client uses. Re-exported from here
 * so the components that render a message keep importing it from beside themselves.
 */
export type {
  Message,
  MessageRole,
  MessageSegment,
  ToolCall,
  ToolCallStatus,
} from "@business-operations/run-client"

import type { ToolCallStatus } from "@business-operations/run-client"

/** How a tool card is rendered — a view concern, so it stays here. */
export interface ToolRenderProps {
  name: string
  args: string
  status: ToolCallStatus
  result?: string
}
