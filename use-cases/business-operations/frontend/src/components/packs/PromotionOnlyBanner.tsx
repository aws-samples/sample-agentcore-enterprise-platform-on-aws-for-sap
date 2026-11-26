// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { PackageCheck } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { usePromotionOnly } from "@/hooks/usePack"

/**
 * One banner on Packs, every pack and every procedure in a stack that takes packs by
 * promotion only (`deployment.promotion_only`): reading stays open, and the edits the API
 * would refuse are disabled with the reason in their tooltip.
 */
export function PromotionOnlyBanner() {
  if (!usePromotionOnly()) return null
  return (
    <Alert role="status">
      <PackageCheck />
      <AlertTitle>This stack takes packs by promotion only</AlertTitle>
      <AlertDescription>
        You can read every pack and SOP here, but not change them. A pack arrives here as a version
        promoted from a development stack.
      </AlertDescription>
    </Alert>
  )
}
