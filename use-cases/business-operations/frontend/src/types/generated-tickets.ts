// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// GENERATED from use-cases/business-operations/types/tickets.schema.json. Do not edit:
// change the schema, then run `deploy.sh usecase run business-operations types`.

/**
 * One ticket in the sample ticketing table.
 */
export interface Ticket {
  /**
   * Unique ticket identifier (e.g. TKT-A1B2C3D4).
   */
  ticket_id: string
  /**
   * Short summary of the ticket.
   */
  title: string
  /**
   * Detailed description of the issue or request.
   */
  description: string
  status: TicketStatus
  priority: TicketPriority
  /**
   * User or agent that created the ticket.
   */
  created_by?: null | string
  /**
   * Person or team assigned to the ticket.
   */
  assigned_to?: null | string
  /**
   * Related ERP case identity for correlation — opaque; what the case is about lives in the case
   * record's `key`. Built and parsed only via the case_key codec.
   */
  case_id?: null | string
  /**
   * Ticket category (e.g. approval_request, exception, inquiry).
   */
  category?: null | string
  /**
   * Expected response type: 'approval' shows approve/deny buttons, 'free_text' shows a reply
   * textarea.
   */
  response_type?: ResponseType
  /**
   * Resolution notes.
   */
  resolution?: null | string
  /**
   * OIDC `sub` of the principal who recorded the decision, from the trusted authorizer context. The
   * agent write gate reads it to refuse an approval with no human behind it.
   */
  decided_by?: null | string
  /**
   * That principal's group claims at the moment of the decision. The agent write gate requires one
   * of them to be in the pack's `approve_groups` before an approval releases a payment.
   */
  decided_by_groups?: string[]
  /**
   * Chronological list of comments on the ticket.
   */
  comments?: TicketComment[]
  /**
   * ISO timestamp of creation.
   */
  created_at: string
  /**
   * ISO timestamp of last update.
   */
  updated_at: string
  /**
   * The id of the case's decision option the reviewer chose, when the decision was one.
   */
  option?: string
}

/**
 * Expected response type: 'approval' shows approve/deny buttons, 'free_text' shows a reply
 * textarea.
 */
export enum ResponseType {
  Approval = "approval",
  FreeText = "free_text",
}

/**
 * Ticket lifecycle status.
 */
export enum TicketStatus {
  Open = "open",
  Assigned = "assigned",
  Approved = "approved",
  Denied = "denied",
  Replied = "replied",
  Closed = "closed",
}

/**
 * Ticket priority level.
 */
export enum TicketPriority {
  High = "high",
  Medium = "medium",
  Low = "low",
}

export interface TicketComment {
  /**
   * Comment author (user alias or 'agent').
   */
  author: string
  /**
   * Comment body.
   */
  text: string
  /**
   * ISO timestamp.
   */
  timestamp: string
}
