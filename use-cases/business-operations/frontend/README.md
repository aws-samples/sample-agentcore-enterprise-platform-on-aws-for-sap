<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# The operator console

The React and Vite operator console for the SAP exception handling use case
(`business-operations`). The use case's AWS Cloud Development Kit (CDK) code deploys it:

- the console stack builds this directory and publishes it to Amazon S3 behind Amazon CloudFront,
  with a `BucketDeployment`;
- the use case's runtime stack writes `aws-exports.json` beside it.

There is no deploy script.

## Development

Node.js 20.19+, 22.12+ or 24, and npm. `doctor` checks that both are installed and prints their
versions; it does not check the version range.

```bash
npm ci
npm test            # vitest, once
npx tsc --noEmit    # typecheck
npm run build       # production build into build/; fails on a chunk over 500 kB
npm run dev         # dev server on http://localhost:3000
npm run dev:mock    # the same, against made-up data with a fake sign-in
```

### Mock mode

`npm run dev:mock` runs the console with no AWS and no SAP. An in-browser API
([MSW](https://mswjs.io/)) serves made-up cases and the shipped `finance_ops` pack, with its rules,
standard operating procedures (SOPs) and example cases. It serves them on the API's resource tree
(`/packs/{pack_id}/…`, `/deployment/…`), and a fake sign-in replaces the identity provider. What
each role may do follows the `access` of `GET /registry`, as a development stack decides it.

A **Mock data** pill in the header, left of search, says that the data is mock data. Open it to
switch:

- who you are signed in as: operator, process analyst, approver, case worker, or someone with no
  pack access;
- the data: the demo cases, or a new deployment with none;
- the stack: a development stack, or a quality stack that takes packs by promotion only.

Changes last until you reload the page. A run replays a recorded run (`src/mocks/runs.ts`, from
`evals/trace_fixtures/`) on a mock run channel, through the same run client and transcript as a
real run. Process, a decision, Rehearse and the assistant stream their steps and words at a
steady pace, and leave the case where the run ends.

Mock mode is for building and reviewing screens. The fake sign-in admits anyone, so:

- mock mode starts only in a development build served on `localhost`;
- the dev server listens only on the loopback address;
- the build fails if any mock code reaches it (`noMockInBuild` in `vite.config.ts`).

The data is in `src/mocks/fixtures.ts`. `test/drift/mock-handlers.test.ts` fails when a service
calls a path that mock mode does not serve.

As shipped, the dev server cannot sign in to a deployed stack. It needs:

- a `public/aws-exports.json`, copied from the console URL. Without it, the app stops at
  "Failed to load authentication configuration";
- a user pool app client whose callback URLs include `http://localhost:3000`;
- an API whose cross-origin resource sharing (CORS) settings admit that origin.

The console client admits only the console URL and the loopback address of the authoring MCP
(Model Context Protocol) server, `http://localhost:12800/callback` (`business_operations/api.py`).
The API admits only the console's origin. No setting adds another. So use `npm test` and
`npm run build` locally, and deploy the console stack to try a change against a deployed API.

## Project structure

```
frontend/
├── src/
│   ├── main.tsx            # Application entry point
│   ├── App.tsx             # Root component with routing
│   ├── routes/             # One component per route: CasesPage, CasePage, PacksIndexPage, PackPage, ProcedurePage, RehearsePage, …
│   ├── components/
│   │   ├── ui/             # shadcn/ui primitives, as the registry ships them
│   │   ├── layout/         # The shell: sidebar, header, search, assistant, page templates
│   │   ├── chat/           # Chat UI
│   │   ├── case/           # Case detail, including the proposed SAP write
│   │   ├── packs/          # A pack's parts, the SOP reader and the lazy SOP editor
│   │   └── auth/           # Authentication components
│   ├── hooks/              # useAgentChat, the chat transcript store, and other hooks
│   ├── lib/                # Utilities: auth, briefing, registry, signOut, statusTone, …
│   ├── services/           # API clients
│   ├── styles/             # globals.css: every design token
│   └── types/              # Case and ticket types, generated from `../types/*.schema.json` by `deploy.sh usecase run business-operations types`
├── test/drift/             # Repo-consistency tests
├── public/                 # Static assets (aws-exports.json is written at deploy)
├── index.html
└── package.json

../packages/run-client/  # The run protocol, framework-free; this app is one client
```

The run protocol is not in this app. It is in `packages/run-client/`, imported as
`@business-operations/run-client`, and covers:

- subscribing to the run event channel;
- `seq` deduplication and out-of-order buffering;
- the AG-UI (Agent-User Interaction protocol) reducer;
- trace replay.

Vite compiles the package with the app; it has no build step of its own.
`src/hooks/useAgentChat.ts` is the React wrapper over it.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Start the Vite dev server |
| `npm run dev:mock` | Start it in mock mode, against made-up data |
| `npm run build` | Type-check with `tsc`, then build for production |
| `npm run preview` | Serve the production build locally |
| `npm run test` | Run the Vitest suite once |
| `npm run test:watch` | Run Vitest in watch mode |
| `npm run test:e2e` | Run the browser tests on mock mode (`PW_CHANNEL=chrome` uses the installed Chrome) |
| `npm run lint` / `npm run lint:fix` | Run ESLint, or run it and fix |
| `npm run clean` | Remove build output and `node_modules` |

## Design conventions

The look is defined in `src/styles/globals.css`. Components use its semantic tokens, never literal
colors or sizes. The rules the codebase enforces, in brief:

1. The visual identity is defined in tokens.
2. Typography uses two faces; mono only for identifiers.
3. Text color never takes an alpha modifier; quieter text is `text-muted-foreground`, and
   `--subtle-foreground` is for marks that are not text, never for text.
4. Page chrome is defined in the layout route.
5. The chat transcript is a module store, not layout-route state.
6. Sign-out drops the operator's work and keeps the window.
7. Empty, loading and error states are the stock components.
8. Color means state, nothing else (`src/lib/statusTone.ts`).
9. Both themes ship, and both are measured.
10. One density: a table row is one line.
11. Chrome sizing is defined in tokens.
12. Motion is limited.
13. Links are styled where they are rendered.
14. Tokens over literals.
15. No build chunk over 500 kB.

These checks enforce the rules as errors:

- `test/drift/config.test.ts`: rules 3 and 10, and that the fonts are self-hosted;
- `test/drift/contrast.test.ts`: rule 9;
- ESLint (`eslint/console-rules.mjs`): rule 1. `npx eslint src/ --max-warnings 0` must pass.

Why each rule exists, and the layout invariants, are in [REFERENCE.md](REFERENCE.md).
