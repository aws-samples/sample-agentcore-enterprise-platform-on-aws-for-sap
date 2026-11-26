// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Re-export generated types (source of truth: types/cases.schema.json)
// Status/Mode/Outcome are renamed: unqualified, they read as case-level state
// beside CaseStatus, and they are neither.
export {
  CaseStatus,
  EvidenceKind,
  UserRating,
  Mode as AuthzMode,
  Outcome as AuthzOutcome,
  Priority,
  Status as SegmentStatus,
  Type as TraceSegmentType,
  Type,
  WriteOp,
} from "./generated-cases"
export type {
  ActionLogEntry,
  AgentTrace,
  CaseRating,
  Citation,
  Evidence,
  EvidenceAuthz,
  EvidenceField,
  EvidenceSource,
  LiveRun,
  ProposedField,
  ProposedWrite,
  TraceSegment,
  WorkItem,
} from "./generated-cases"

import { CaseStatus } from "./generated-cases"
import type { StatusTone } from "@/lib/statusTone"

// UI-only display metadata — not part of the schema
export const CASE_STATUSES = Object.values(CaseStatus)

// `tone` selects the shared state colour — see lib/statusTone.ts.
export const STATUS_META: Record<CaseStatus, { label: string; tone: StatusTone }> = {
  [CaseStatus.Detected]: { label: "Detected", tone: "info" },
  [CaseStatus.Processing]: { label: "Processing", tone: "progress" },
  [CaseStatus.AwaitingHumanInput]: { label: "Awaiting input", tone: "attention" },
  // Written to SAP but not yet closed out, so it shares the "finished well"
  // tone with Complete and is distinguished by its label.
  [CaseStatus.SapUpdated]: { label: "SAP updated", tone: "success" },
  [CaseStatus.Complete]: { label: "Complete", tone: "success" },
  [CaseStatus.ManualReviewRequired]: { label: "Manual review", tone: "danger" },
  [CaseStatus.Error]: { label: "Error", tone: "danger" },
}

/** Display metadata for a case status, tolerating unknown values from older records. */
export function caseStatusMeta(status: CaseStatus | string): { label: string; tone: StatusTone } {
  return STATUS_META[status as CaseStatus] ?? STATUS_META[CaseStatus.Detected]
}
