// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ComponentProps } from "react"
import { cn } from "@/lib/utils"

/**
 * An identifier a person copies: a constant name, a SAP field or entity, a key, a file name.
 * Mono, one step below body text and muted, so it sits beside the words it names without
 * standing out. Never for prose or values: those are sans.
 */
export function Ident({ className, ...props }: ComponentProps<"code">) {
  return <code className={cn("font-mono text-xs text-muted-foreground", className)} {...props} />
}
