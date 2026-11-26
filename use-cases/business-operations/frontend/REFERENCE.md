<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Console reference

This page is for anyone who changes the console. [README.md](README.md) says how to build and test
it. This page says why it is built the way it is. Each rule exists because breaking it once caused
a defect.

## Test-enforced rules

`test/drift/` fails on any of these:

- `config.test.ts`:
  - `text-*` never takes an alpha modifier outside `components/ui`;
  - the type scale is Tailwind's own, with nothing below 12 px and no density class;
  - the theme script stays a same-origin file that runs before the app bundle;
  - both font faces are self-hosted;
  - the chrome widths are tokens.
- `console-rules.test.ts`: the class matcher behind the lint rule `console/banned-classes`.
- `contrast.test.ts`: every tone on its wash, each tone as text, the status dots and the focus
  ring, measured from the tokens in both themes: 4.5:1 for text, 3:1 for a mark.
- `mock-handlers.test.ts`: mock mode serves every path a service calls.
- `services-openapi.test.ts`: every API path a service calls is one `../openapi.yaml` declares.
- `env-access.test.ts`: no `process.env` read in the bundle. Vite substitutes only
  `import.meta.env.VITE_*`, and a `process.env.X` read returns `undefined` with no error.

`npm run build` runs `tsc` and fails on a chunk over 500 kB. `npx eslint src/ --max-warnings 0`
fails on any breach of the design rules below, because they are lint errors. The Vitest tests
are beside the code they cover, under `src/`.

The browser tests in `e2e/` run on mock mode:

- `screens.spec.ts` renders every page in both themes with axe, and takes the screenshots;
- `flows.spec.ts` goes through what a person does;
- `keyboard.spec.ts` tabs through every page, with a visible focus at each stop and no keyboard
  trap. It also checks the shortcuts, Escape out of each sheet and dialog, the live regions, and
  axe's landmark and heading rules.

## Layout invariants

- **Routes.** `/` and `/workspace` redirect to `/cases`; `/cases/:caseId` is one case. `/packs`
  is the packs index, `/packs/:packId?tab=` a pack by part or its Versions,
  `/packs/:packId/procedures/:processType` a procedure (`/instructions` the general instructions)
  and `/packs/:packId/rehearse` a rehearsal. `/tickets` is the tickets page, with sample
  ticketing only, and `/deployment` the deployment settings. Every route is a `PageFrame` child
  and has its own scroll area. Any other path goes to `/cases`.
  The app runs on a data router (`App.tsx`) with one catch-all route, so a page with unsaved work
  can stop navigation with `useBlocker`.
- **The shell.** `routes/AppShell.tsx` owns the sidebar (`components/layout/AppSidebar.tsx`), the
  header band, search (⌘K), the toasts and the assistant. The assistant is the ask bar and the
  panel. The panel opens only when an answer you asked for arrives in it, and closes only when you
  close it. A rehearsal does not open it: its steps show on the Rehearse page.
- **Runs show on the case.** Process starts the run from the case page, and its steps stream onto
  that page. The assistant stays as it was.
- **Pack pages read the API's parts.** A pack's tabs read `GET /packs/{pack_id}` and the part
  routes under it (`services/`). What a person may do in a pack comes from the `access` of
  `GET /registry` (`lib/packParts.ts`). What they may do across the deployment (create a pack, edit
  the contacts) comes from the `access` of `GET /deployment/settings`.
  - In a stack that takes packs by promotion only (`deployment.promotion_only`),
    `PromotionOnlyBanner` says so on Packs, on every pack and on every standard operating
    procedure (SOP). Each edit that the API would refuse gives that as its reason.
  - A control that a person cannot use is a `ReasonButton`: disabled, focusable, with the reason in
    a tooltip.
  - No field, group or rule name of a pack is written in the console's code.
- **One spelling for each kind of value.**
  - Times go through `components/When.tsx`: relative within a day, then a short date, with the full
    time in a tooltip.
  - Amounts have two decimals, with the ISO currency after the number.
  - SAP fields show as the pack's labels (`lib/evidence.ts`), with the SAP name in a tooltip.
  - Ids made readable go through `humanise` in `lib/registry.ts`, in sentence case with acronyms
    kept.
- **The brand comes from the deployment.** `hooks/useBrand.ts` reads `console.brand` from
  `aws-exports.json` (its name, subtitle and monogram). If it is not set, the hook uses
  `src/brand.ts`, which also contains the logo and the release number.
- **Bands across the page.** Above the header, `StopBand` (in `components/EmergencyStop.tsx`) shows
  while an emergency stop is in effect. Below it, `SandboxBand` shows on a non-production
  deployment. Each reads `GET /deployment/settings`, so neither shows before sign-in.
- **Shared preferences.** `hooks/usePreference.ts` is a `localStorage` value that every reader
  shares (starred packs, sandbox markers). `hooks/useStoredState.ts` is a value that only its
  component reads.
- **Counts are not model output.** The briefing's counts come from `lib/digest.ts` and its prompt
  from `lib/briefing.ts`; the view counts and status words from `lib/caseViews.ts`.
- **The run protocol is not here.** The channel, ordering and the reducer are
  `../packages/run-client/`. `hooks/useAgentChat.ts` connects it to React, and subscribes only to
  the channels that the run-event check admits ([runtime reference](../docs/reference/runtime.md)).

## Design rules

1. **The visual identity is defined in tokens.** `src/styles/globals.css` is the one place where
   the look is defined. `:root` is light, `.dark` is dark, and `@theme inline` maps each token to a
   utility. The palette is Radix gray, ink on off-white, with a flat shell. Colour is used only for
   the four status tones.

   The components in `components/ui` are stock shadcn on Base UI (`components.json`, style
   `base-nova`). They are added with `npx shadcn add` and left as the registry ships them. Pages
   compose them and set no colour, size or radius of their own, which `eslint/console-rules.mjs`
   checks. A registry component is changed only for a defect, with a comment saying why
   (`tabs.tsx` for contrast and a focus ring, `command.tsx` for its dialog title).
2. **Typography uses two faces.** `font-sans` (Geist) is for everything, headings included; size
   and weight give the display voice. `font-mono` (Geist Mono) is only for identifiers that people
   compare character by character against SAP: document numbers, case ids, keys, file paths and
   JSON. It is never for labels, amounts or dates. Sizes are Tailwind's own scale, 12 px (`text-xs`)
   the smallest. Money and metric columns are `tabular-nums`.
3. **Text colour never takes an alpha modifier.** `--muted-foreground` reaches 4.5:1 with very
   little margin, so any opacity puts it below the WCAG AA level. De-emphasis is a token, never a
   multiplier: text that should look quieter is `text-muted-foreground`. `--subtle-foreground` is
   only for marks that are not text (a chevron at rest, a separator), judged at the 3:1 level for
   graphics. Never set text in it.

   Surfaces are the stock `muted`, `accent` and `sidebar` tokens. Alpha is still allowed on
   graphical objects such as focus rings. The tokens are written out per theme, not derived with
   `color-mix()`. On a browser engine without `color-mix()`, the Lightning CSS fallback paints solid
   ink.
4. **Page chrome is defined in the layout route.** `AppShell` gives every route the sidebar and the
   header band; `PageFrame` gives the column below it, and `components/layout/DetailLayout.tsx`
   the main-and-side template. A route renders its body and inherits the rest.
5. **The chat transcript is a module store, not layout-route state.** `hooks/transcript.ts` is a
   `useSyncExternalStore` store the assistant subscribes to directly, so a streamed token repaints
   the assistant and nothing else. `hooks/agentActivity.ts` uses the same pattern for the sidebar's
   agent status. Both stay after their component unmounts, so a test that renders the shell resets
   them. Messages are never persisted, because they contain SAP tool results.
6. **Sign-out drops the work and keeps the window.** `lib/signOut.ts` is the one place that clears
   data, on Logout and when a session expires. It clears the transcript, the query cache, the
   case-scoped `localStorage` keys and the query string that names the focused case. For
   `localStorage` it keeps only an allowlist of layout keys, so a key added later is dropped by
   default. `oidc.*` is left to oidc-client-ts.
7. **Empty, loading and error states are stock.** They use `Empty`, `Skeleton` and `Alert` from
   `components/ui`. A disclosure is `Collapsible`, and a tooltip is `Tooltip`. A tooltip is never a
   `title` attribute, which keyboard and touch users never see.
8. **Colour means state, nothing else.** `lib/caseViews.ts` maps a case's status onto a tone, and
   `lib/statusTone.ts` maps the agent's marks and the timeline's marks. A table shows a status as a
   dot and a word. The tones are `--tone-*` in `globals.css`:
   - `progress`, violet, only while the agent works;
   - `attention`, amber, when a person is needed;
   - `success`, jade;
   - `danger`, red.

   Each tone has a dot, a wash and the ink on that wash. Success and danger also have a text step.
   No tone has a palette weight or a `dark:` variant. `--marker` is the highlighter under a quote
   verified against the procedure (`bg-marker`). A trigger source is a neutral outline badge with
   its icon.
9. **Both themes ship, and both are measured.** `useTheme` sets `.dark` on
   `document.documentElement`, so popups rendered in portals follow it. A blocking script in
   `index.html` sets it before first paint. Every colour that has a meaning reaches 4.5:1 as text and
   3:1 as a graphical object, which `contrast.test.ts` measures.
10. **One density.** There is no density class: a table row is one line, about 36 px, and type
    never shrinks.
11. **Chrome sizing is defined in tokens.** `--header-height`, `--toolbar-height`, `--assistant-width`
    and `--gutter` in `globals.css`; the sidebar's widths are the stock component's.
12. **Motion is limited.** One easing curve (`--ease-standard`) and three durations
    (`--duration-fast` 120 ms, `--duration-base` 200 ms, `--duration-slow` 250 ms, the ceiling).
    Motion shows causality and is never decoration; pair a transition with
    `motion-reduce:transition-none`, and give a keyframe animation an entry in the
    `prefers-reduced-motion` block.
13. **Links are styled where they are rendered.** There is no global `a` rule. An earlier global
    rule underlined every router `<Link>` around a button, and failed contrast. Prose links in the
    chat's Markdown (`components/chat/Markdown.tsx`) use the `--link` token, set per theme.
    Markdown written by the agent is untrusted, so its external links open with
    `rel="noopener noreferrer"`.
14. **Tokens over literals.** Colours are declared once, in `:root` and `.dark`, and reached
    through utilities; the status tones are `--tone-*` there.
15. **No build chunk over 500 kB.** The `chunk-budget` plugin in `vite.config.ts` fails the build,
    because people easily miss a Rollup size warning in the build output.
