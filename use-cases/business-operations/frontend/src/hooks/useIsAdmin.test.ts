// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest"
import { accessTokenRoles } from "./useIsAdmin"

const b64url = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
const jwt = (claims: object) => `h.${b64url(JSON.stringify(claims))}.s`

describe("accessTokenRoles", () => {
  it("reads the app roles off an Entra access token", () => {
    expect(accessTokenRoles(jwt({ roles: ["business-operations-admins"] }))).toEqual([
      "business-operations-admins",
    ])
  })

  it("is empty for a token with no roles, a malformed one, or none", () => {
    expect(accessTokenRoles(jwt({ scp: "x" }))).toEqual([])
    expect(accessTokenRoles("not-a-jwt")).toEqual([])
    expect(accessTokenRoles(undefined)).toEqual([])
  })
})
