<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# `@business-operations/run-client`

A TypeScript client for following a run of the business process agent as it streams, with no
dependencies and no framework. Use it to start and follow this use case's agent from something
other than the browser.

AWS AppSync Events does not guarantee ordering or single delivery. So this package removes
duplicates by `seq`, holds back frames that arrive early, and merges the run under way onto the
stored case record. The browser app uses the same package.

## Contents

| Module | What it owns |
|--------|--------------|
| `src/channel.ts` | `subscribeRunEvents` — the `aws-appsync-event-ws` handshake, subscribe, keepalive watchdog and reconnect. Socket injectable. |
| `src/runFollower.ts` | `RunFollower` — duplicate removal by `seq`, buffering of frames that arrive out of order, one projection per slot, the splice, and hiding of a dismissed run. |
| `src/reducer.ts` | `reduceAguiEvent` — one AG-UI event folded into a chat projection, purely. |
| `src/traces.ts` | `storedTurns` / `liveTurn` / `isLiveRunStale` — a case record replayed as a transcript, including the run still under way. |
| `src/prompt.ts` | `buildPromptWithHistory` / `stripPromptScaffolding` — the prompt envelope a chat turn includes. |
| `src/types.ts` | `Message`, `ToolCall`, `AgentTrace`, `LiveRun` — structural, so generated types pass straight in. |
| `src/generated-contract.ts` | `LIVE_RUN_STALE_SECONDS`, generated from `../../types/` by `deploy.sh usecase run business-operations types`. Do not edit it. |

The REST side of the contract is deliberately not wrapped, because it is a few `fetch` calls.
They are described in the use case's `openapi.yaml` (`use-cases/business-operations/openapi.yaml`).

## Usage

```ts
import { RunFollower, subscribeRunEvents } from "@business-operations/run-client"

const transcripts = new Map<string, Message[]>()

const follower = new RunFollower({
  readTranscript: slot => transcripts.get(slot) ?? [],
  writeTranscript: (slot, update) => transcripts.set(slot, update(transcripts.get(slot) ?? [])),
  onFrame: ({ turn }) => render(turn),
  onRunEnd: slot => refetchCase(slot)
})

const subscription = subscribeRunEvents({
  httpDomain,
  realtimeDomain,
  // A case's channel names its pack by the short id its case ids start with;
  // a run with no case publishes on `/runs/<session id>`.
  channel: `/runs/${caseId.split("-")[0]}/${caseId}`,
  getIdToken: async () => idToken,
  onEvent: envelope => follower.accept(envelope)
})
```

A **slot** is the key a run publishes under: the case id, on `/runs/<short_id>/<case_id>`, or
the run's session id, on `/runs/<session_id>`, when there is no case. The subscribe check admits
`/runs/<short_id>/<case_id>` and `/runs/<short_id>/*` only for a pack the caller may see, and
refuses `/runs/*` and a bare `/runs/<case_id>`. One follower handles every slot it is given.

`examples/follow-run.ts` shows the whole flow on bare Node. It starts a run over `POST /runs`,
and follows it to the end. A run is a question unless it says otherwise. `--process` sends
`intent: "process"`, which runs the case's standard operating procedure (SOP) and may write to
SAP. Without `--process`, the agent reads and answers, and changes nothing:

```bash
node use-cases/business-operations/packages/run-client/examples/follow-run.ts --help
```

The example needs **Node 22.18 or later**, which runs TypeScript sources directly. The console
that imports this package needs Node 20.19+, 22.12+ or 24 ([frontend/README.md](../../frontend/README.md)).

## No build step

The package ships TypeScript sources only. There is no workspace, bundler or dependency to install.

- **The browser app** imports it as `@business-operations/run-client`, aliased to
  `src/index.ts`. Vite compiles it with the app.
- **Node 22.18+** runs the sources directly. Do not use `enum` or constructor parameter
  properties in this package, because Node's type stripping rejects both.

## Tests

```bash
cd use-cases/business-operations/frontend && npx vitest --run ../packages
```

## Internals

The run-event channel, its delivery guarantees and the envelope format are in
[docs/reference/runtime.md](../../docs/reference/runtime.md).
