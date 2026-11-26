// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react"
import { getConfig } from "@/lib/config"

/** Which optional demo features are deployed (gates the Tickets UI); false until config resolves. */
export function useDemoFeatures(): { ticketing: boolean; known?: boolean } {
  // `known` is false until the config has been read, so a deep link to a gated route can
  // wait for the answer instead of being redirected away from it.
  const [features, setFeatures] = useState({ ticketing: false, known: false })

  useEffect(() => {
    let active = true
    getConfig()
      .then(cfg => {
        if (active) setFeatures({ ticketing: cfg.ticketingEnabled, known: true })
      })
      .catch(() => {
        if (active) setFeatures({ ticketing: false, known: true })
      })
    return () => {
      active = false
    }
  }, [])

  return features
}
