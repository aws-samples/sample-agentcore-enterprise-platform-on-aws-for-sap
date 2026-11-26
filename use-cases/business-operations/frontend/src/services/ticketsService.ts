// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { Ticket, TicketStatus } from "@/types/tickets"
import { getConfig } from "@/lib/config"
import { apiFetch } from "@/lib/apiFetch"

/** One page of `GET /tickets?limit=…`; `next_token` is null on the last one. */
interface TicketPage {
  items: Ticket[]
  next_token: string | null
}

// The route's ceiling. Every caller here wants the whole list (the briefing counts it),
// so this follows `next_token` to the end rather than showing a first page.
const PAGE_LIMIT = "500"

async function ticketsBase(): Promise<string> {
  const { apiUrl, ticketingEnabled } = await getConfig()
  if (!ticketingEnabled) throw new Error("Sample ticketing is not deployed")
  return apiUrl
}

export async function fetchTickets(
  filter: { status?: TicketStatus | "all"; assigned_to?: string },
  token: string
): Promise<Ticket[]> {
  const apiUrl = await ticketsBase()
  const params = new URLSearchParams()
  if (filter.status && filter.status !== "all") params.set("status", filter.status)
  if (filter.assigned_to) params.set("assigned_to", filter.assigned_to)
  params.set("limit", PAGE_LIMIT)

  const tickets: Ticket[] = []
  for (;;) {
    const page: TicketPage = await apiFetch(
      `${apiUrl}/tickets?${params}`,
      { token },
      "Failed to fetch tickets"
    )
    tickets.push(...page.items)
    if (!page.next_token) break
    params.set("next_token", page.next_token)
  }
  // The server orders within a page only — the status index has no sort key.
  return tickets.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
}

export async function fetchTicket(ticketId: string, token: string): Promise<Ticket> {
  const apiUrl = await ticketsBase()
  return apiFetch(`${apiUrl}/tickets/${ticketId}`, { token }, "Failed to fetch ticket")
}

export async function createTicket(
  data: {
    title: string
    description: string
    priority?: string
    assigned_to?: string
    category?: string
  },
  token: string
): Promise<Ticket> {
  const apiUrl = await ticketsBase()
  return apiFetch(
    `${apiUrl}/tickets`,
    { token, method: "POST", body: data },
    "Failed to create ticket"
  )
}

export async function updateTicket(
  ticketId: string,
  data: {
    status?: string
    assigned_to?: string
    resolution?: string
    comment?: string
    comment_author?: string
  },
  token: string
): Promise<Ticket> {
  const apiUrl = await ticketsBase()
  return apiFetch(
    `${apiUrl}/tickets/${ticketId}`,
    { token, method: "PUT", body: data },
    "Failed to update ticket"
  )
}

export async function submitTicketAction(
  ticketId: string,
  action: "approved" | "denied" | "replied",
  resolution: string,
  token: string,
  responseText?: string,
  /** The chosen option's id, on a case with decision options (2.23); `action` is then unused. */
  optionId?: string
): Promise<{
  ticket: Ticket
  enqueued: boolean
  case_id: string
  awaiting_second_approver?: boolean
}> {
  const apiUrl = await ticketsBase()
  return apiFetch(
    `${apiUrl}/tickets/${ticketId}/action`,
    {
      token,
      method: "POST",
      body: {
        // An option is chosen instead of an action (2.23): the route refuses both together.
        ...(optionId ? { option: optionId } : { action }),
        resolution,
        // The reviewer's own words, on every action. `resolution` is what reaches the
        // resumed agent, so a canned comment here would only diverge from it.
        comment: resolution,
        ...(responseText ? { response_text: responseText } : {}),
      },
    },
    "Failed to submit ticket action"
  )
}
