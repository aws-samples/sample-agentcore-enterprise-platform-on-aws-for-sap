// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

export interface ApiFetchInit extends Omit<RequestInit, "body"> {
  token: string
  body?: unknown
}

/** Renews the session and answers the fresh API token, or undefined when it cannot. */
type Renew = () => Promise<string | undefined>
let renew: Renew | null = null

/**
 * Register how a 401 renews the session. The auth tree calls this once; `apiFetch` itself
 * holds no auth state. The API Gateway's own 401 carries the console origin, so an expired
 * token reaches here as a status rather than as "Failed to fetch".
 */
export function onUnauthorized(fn: Renew | null): void {
  renew = fn
}

/**
 * The API's own error body as one sentence: `{ error, detail }` joined, or every entry of
 * `details` when it names the fields it refused. Pass as `parseError`.
 */
export async function readApiError(res: Response): Promise<string | undefined> {
  const body = await res.json().catch(() => ({}))
  if (Array.isArray(body.details) && body.details.length) return body.details.join("; ")
  return [body.error, body.detail].filter(Boolean).join(": ") || undefined
}

/** The JSON body an `apiFetch` error came with, when there was one. */
export function errorBody(error: unknown): Record<string, unknown> | undefined {
  const body = (error as { body?: unknown } | null)?.body
  return body && typeof body === "object" ? (body as Record<string, unknown>) : undefined
}

/** The HTTP status an `apiFetch` error carries, when it came from a response. */
export function errorStatus(error: unknown): number | undefined {
  const status = (error as { status?: unknown } | null)?.status
  return typeof status === "number" ? status : undefined
}

/**
 * Fetch JSON from the API with the bearer token attached.
 *
 * A 401 is answered once by a silent sign-in (see {@link onUnauthorized}) and the request
 * retried with the fresh token; a second 401 is an error like any other.
 *
 * Throws `${errorMessage}: ${status}` on a non-2xx response by default. Pass
 * `parseError` to extract a more specific message from the error body first
 * (e.g. a JSON `{ error }` field or raw text) — used where the backend returns
 * detail worth surfacing verbatim instead of just a status code.
 */
export async function apiFetch<T>(
  url: string,
  { token, body, headers, ...init }: ApiFetchInit,
  errorMessage: string,
  parseError?: (res: Response) => Promise<string | undefined>
): Promise<T> {
  const send = (bearer: string) =>
    fetch(url, {
      ...init,
      headers: {
        Authorization: `Bearer ${bearer}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  let res = await send(token)
  if (res.status === 401 && renew) {
    const fresh = await renew().catch(() => undefined)
    if (fresh) res = await send(fresh)
  }
  if (!res.ok) {
    const raw: unknown =
      typeof res.clone === "function"
        ? await res
            .clone()
            .json()
            .catch(() => undefined)
        : undefined
    const detail = parseError ? await parseError(res).catch(() => undefined) : undefined
    // The status rides on the error, so a caller can tell a 409 (the record moved on) from
    // a refusal without parsing the sentence.
    throw Object.assign(new Error(detail || `${errorMessage}: ${res.status}`), {
      status: res.status,
      body: raw,
    })
  }
  if (res.status === 204) return undefined as T
  return res.json()
}
