// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The people mock mode can sign in as. Groups are the shipped finance_ops pack's
 * entitlement groups and the deployment's admins group, so the console's own checks
 * (admin links, who may approve, who may author) decide what each one sees.
 */
export const ADMINS_GROUP = "business-operations-admins"
/** Who may lower a pack's write limits in a stack (round six, task 6.2). */
export const LIMIT_OWNERS_GROUP = "business-operations-limit-owners"
/** Who may start Process and Re-run on a case (round seven, task 7.1). */
export const CASE_WORKERS_GROUP = "business-operations-case-workers"

export interface MockRole {
  id: string
  label: string
  name: string
  email: string
  groups: string[]
  /** What `GET /registry` and `GET /sops` say this person may do; the server decides. */
  canAuthor: boolean
}

export const ROLES: MockRole[] = [
  {
    id: "operator",
    label: "Operator",
    name: "Sam Lee",
    email: "sam@example.com",
    groups: [
      ADMINS_GROUP,
      LIMIT_OWNERS_GROUP,
      CASE_WORKERS_GROUP,
      "business-operations-ap-manager",
    ],
    canAuthor: true,
  },
  {
    id: "analyst",
    label: "Process analyst",
    name: "Ana Ruiz",
    email: "ana@example.com",
    groups: ["business-operations-ap-clerk"],
    canAuthor: true,
  },
  {
    id: "approver",
    label: "Approver",
    name: "Priya Shah",
    email: "priya@example.com",
    groups: [CASE_WORKERS_GROUP, "business-operations-ap-manager"],
    canAuthor: false,
  },
  {
    id: "worker",
    label: "Case worker",
    name: "Lee Chen",
    email: "lee@example.com",
    // Starts the agent on a case, and decides nothing: the clerk group reads, not approves.
    groups: [CASE_WORKERS_GROUP, "business-operations-ap-clerk"],
    canAuthor: false,
  },
  {
    id: "none",
    label: "No pack access",
    name: "Alex Kim",
    email: "alex@example.com",
    groups: [],
    canAuthor: false,
  },
]

const KEY = "mock.role"

export function currentRole(): MockRole {
  const id = window.localStorage.getItem(KEY)
  return ROLES.find(r => r.id === id) ?? ROLES[0]
}

export function switchRole(id: string): void {
  window.localStorage.setItem(KEY, id)
  window.location.reload()
}

/** True when this role may see the pack at all, from its entitlement groups. */
export function seesPack(role: MockRole, groups: string[]): boolean {
  return role.groups.includes(ADMINS_GROUP) || role.groups.some(g => groups.includes(g))
}
