// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Ident } from "@/components/Ident"
import { useState } from "react"
import { useInfiniteQuery } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import { JsonBlock } from "@/components/ui/json-block"
import { TruncatedText } from "@/components/TruncatedText"
import { When } from "@/components/When"
import { useApi } from "@/hooks/useApi"
import { packKey, usePackDocument, usePackVersion } from "@/hooks/usePack"
import {
  fetchPackVersions,
  type PackVersion,
  type PackVersionDocument,
  type VersionOrigin,
} from "@/services/versionsService"
import { formatRule, ruleLabel, VERSION_PART_LABEL } from "@/lib/packParts"
import { humanise, type UseCasePack } from "@/lib/registry"
import { versionDiff, type VersionChange } from "@/lib/versionDiff"
import { LineDiffView } from "@/components/packs/LineDiffView"

/** How a version was made, in words: the verb that minted it. */
const MADE_VIA: Record<string, string> = {
  edit: "Edited here",
  create: "Created here",
  export: "Exported",
  seed: "From the checkout",
  import: "Imported",
  adopt: "Adopted",
}

/** How the version became active in this stack. */
function originWords(o: VersionOrigin | null): string {
  if (!o) return "Not recorded"
  switch (o.via) {
    case "edit":
      return "An edit here"
    case "create":
      return "Created here"
    case "seed":
      return "Pack import at deploy"
    case "import":
      return o.change_ref ? `Imported, change ${o.change_ref}` : "Imported"
    case "adopted":
      return "Adopted at the first deploy"
    default:
      return o.via
  }
}

/**
 * Versions (2.26): every version this stack stores of the pack, newest first, with who made it,
 * when, the parts it changed and how it became active here. Opening one shows what it changed
 * against the version before it, by part and then by key, and its document. Read only: a
 * version is made by saving a part, or arrives by promotion.
 */
export function VersionsPart({ pack }: { pack: UseCasePack }) {
  const auth = useAuth()
  const getToken = useApi()
  const [opened, setOpened] = useState<number | null>(null)
  const list = useInfiniteQuery({
    queryKey: packKey(pack.id, "versions"),
    queryFn: async ({ pageParam }) => fetchPackVersions(pack.id, await getToken(), pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: last => last.next_token ?? undefined,
    enabled: auth.isAuthenticated,
  })
  const pages = list.data?.pages ?? []
  const active = pages[0]?.active ?? null
  const rows = pages.flatMap(p => p.versions)

  if (list.isError)
    return (
      <Alert variant="destructive">
        <AlertTitle>Couldn&apos;t read the versions</AlertTitle>
        <AlertDescription>{(list.error as Error).message}</AlertDescription>
      </Alert>
    )

  return (
    <div className="flex flex-col gap-4">
      <p className="max-w-measure text-sm text-muted-foreground">
        Each save makes a new version of the whole pack, and a promotion brings one in. This stack
        runs the active version.
      </p>
      {!list.data ? (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 3 }, (_, i) => (
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <Empty>
          <EmptyHeader>
            <EmptyTitle>No versions stored yet</EmptyTitle>
            <EmptyDescription>The first deploy stores the pack as version 0.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <div className="rounded-lg border">
          <Table className="table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="w-40 pl-4">Version</TableHead>
                <TableHead>Changed</TableHead>
                <TableHead className="w-56">By</TableHead>
                <TableHead className="w-28">When</TableHead>
                <TableHead className="w-56 pr-4">Became active</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map(v => (
                <TableRow key={v.version} className="h-9">
                  <TableCell className="pl-4">
                    <Button
                      variant="link"
                      size="sm"
                      className="h-auto p-0 tabular-nums"
                      onClick={() => setOpened(v.version)}
                    >
                      Version {v.version}
                    </Button>
                    {v.version === active && (
                      <Badge variant="secondary" className="ml-2">
                        Active
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <TruncatedText text={partsWords(v)} />
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    <TruncatedText text={v.made_by ?? MADE_VIA[v.via] ?? v.via} />
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    <When at={v.made_at} focusable={false} />
                  </TableCell>
                  <TableCell className="pr-4 text-muted-foreground">
                    <TruncatedText text={originWords(v.origin)} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {list.hasNextPage && (
        <Button
          variant="outline"
          size="sm"
          className="self-start"
          disabled={list.isFetchingNextPage}
          onClick={() => void list.fetchNextPage()}
        >
          {list.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      )}

      <Sheet open={opened != null} onOpenChange={o => !o && setOpened(null)}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 sm:max-w-xl">
          {opened != null && (
            <VersionSheet
              pack={pack}
              version={opened}
              entry={rows.find(r => r.version === opened)}
              active={active}
            />
          )}
        </SheetContent>
      </Sheet>
    </div>
  )
}

function partsWords(v: PackVersion): string {
  if (v.parts.length === 0) return v.via === "create" ? "The first version" : "Nothing"
  return v.parts.map(p => VERSION_PART_LABEL[p] ?? p).join(", ")
}

/** One version's document, as stored: what it changed, then its pack, prompt and SOPs. */
function VersionSheet({
  pack,
  version,
  entry,
  active,
}: {
  pack: UseCasePack
  version: number
  entry?: PackVersion
  active: number | null
}) {
  const doc = usePackVersion(pack.id, version)
  const v = doc.data ?? entry
  const files = v
    ? [
        ...v.changes.files.added.map(f => `${f} (added)`),
        ...v.changes.files.changed,
        ...v.changes.files.removed.map(f => `${f} (removed)`),
      ]
    : []
  return (
    <>
      <SheetHeader className="border-b">
        <SheetTitle className="flex items-center gap-2">
          Version {version}
          {version === active && <Badge variant="secondary">Active</Badge>}
        </SheetTitle>
        <SheetDescription>
          {v ? (
            <>
              {MADE_VIA[v.via] ?? v.via}
              {v.made_by ? ` by ${v.made_by}` : ""}
              {v.made_at ? (
                <>
                  , <When at={v.made_at} />
                </>
              ) : null}
              . Read only.
            </>
          ) : (
            "Read only."
          )}
        </SheetDescription>
      </SheetHeader>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4 text-sm">
        {v && (
          <dl className="flex flex-col gap-2">
            <div className="flex gap-4">
              <dt className="w-32 flex-none text-muted-foreground">Parts changed</dt>
              <dd>{partsWords(v)}</dd>
            </div>
            <div className="flex gap-4">
              <dt className="w-32 flex-none text-muted-foreground">Files</dt>
              <dd className="flex min-w-0 flex-col gap-0.5">
                {files.length ? (
                  files.map(f => (
                    <Ident key={f} className="truncate">
                      {f}
                    </Ident>
                  ))
                ) : (
                  <span className="text-muted-foreground">None</span>
                )}
              </dd>
            </div>
            <div className="flex gap-4">
              <dt className="w-32 flex-none text-muted-foreground">Became active</dt>
              <dd>{originWords(v.origin)}</dd>
            </div>
            {v.made_in && (
              <div className="flex gap-4">
                <dt className="w-32 flex-none text-muted-foreground">Made in</dt>
                <dd>{v.made_in}</dd>
              </div>
            )}
            {v.change_note && (
              <div className="flex gap-4">
                <dt className="w-32 flex-none text-muted-foreground">Note</dt>
                <dd>{v.change_note}</dd>
              </div>
            )}
          </dl>
        )}
        {doc.isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t read this version</AlertTitle>
            <AlertDescription>{(doc.error as Error).message}</AlertDescription>
          </Alert>
        )}
        {!doc.data && !doc.isError && <Skeleton className="h-40 w-full" />}
        {doc.data && <VersionChanges pack={pack} doc={doc.data} />}
        {doc.data && <h3 className="font-medium">Document</h3>}
        {doc.data && (
          <Accordion multiple>
            <AccordionItem value="pack">
              <AccordionTrigger>pack.json</AccordionTrigger>
              <AccordionContent>
                <JsonBlock text={JSON.stringify(doc.data.pack, null, 2)} />
              </AccordionContent>
            </AccordionItem>
            <AccordionItem value="prompt">
              <AccordionTrigger>General instructions</AccordionTrigger>
              <AccordionContent>
                <p className="whitespace-pre-wrap">{doc.data.base_prompt || "None"}</p>
              </AccordionContent>
            </AccordionItem>
            {Object.entries(doc.data.sops).map(([file, text]) => (
              <AccordionItem key={file} value={file}>
                <AccordionTrigger>
                  <Ident className="text-foreground">{file}</Ident>
                </AccordionTrigger>
                <AccordionContent>
                  <p className="whitespace-pre-wrap">{text}</p>
                </AccordionContent>
              </AccordionItem>
            ))}
          </Accordion>
        )}
      </div>
    </>
  )
}

/** A value in a version's diff: a rule in its own units, anything else as compact JSON. */
function valueWords(change: Extract<VersionChange, { kind: "value" }>, v: unknown): string {
  if (v === undefined) return "Not set"
  if (change.symbol) return formatRule(change.symbol, v)
  const text = JSON.stringify(v)
  return text.length > 160 ? `${text.slice(0, 157)}…` : text
}

/**
 * What this version changed against the one before it, read from the two documents: by part,
 * then by key, with a line diff for the general instructions and each SOP.
 */
function VersionChanges({ pack, doc }: { pack: UseCasePack; doc: PackVersionDocument }) {
  const previousVersion = doc.previous?.version ?? null
  const previous = usePackVersion(pack.id, previousVersion)
  const current = usePackDocument(pack.id)
  const sopName = (file: string) => {
    const row = pack.sops?.find(r => r.kind !== "alias" && r.sop.endsWith(`/${file}`))
    return row ? `${humanise(row.process_type)} SOP` : file
  }

  if (previousVersion == null)
    return (
      <p className="text-muted-foreground">
        The first version this stack stores, so there is nothing before it to compare.
      </p>
    )
  if (previous.isError)
    return (
      <Alert variant="destructive">
        <AlertTitle>Couldn&apos;t read version {previousVersion}</AlertTitle>
        <AlertDescription>{(previous.error as Error).message}</AlertDescription>
      </Alert>
    )
  if (!previous.data || !current.data) return <Skeleton className="h-24 w-full" />
  const parts = versionDiff(
    previous.data,
    doc,
    current.data.parts ?? {},
    current.data.constant_parts ?? {}
  )

  return (
    <section aria-labelledby="version-changes" className="flex flex-col gap-3">
      <h3 id="version-changes" className="font-medium">
        Changes from version {previousVersion}
      </h3>
      {parts.length === 0 && (
        <p className="text-muted-foreground">The same content as version {previousVersion}.</p>
      )}
      {parts.map(({ part, changes }) => (
        <div key={part ?? "other"} className="flex flex-col gap-2">
          <h4 className="text-xs font-medium text-muted-foreground">
            {part ? (VERSION_PART_LABEL[part] ?? part) : "Other keys"}
          </h4>
          {changes.map((c, i) =>
            c.kind === "value" ? (
              <div key={i} className="flex flex-col gap-0.5">
                <span>
                  {c.symbol ? ruleLabel(c.symbol) : humanise(c.key)}{" "}
                  <Ident>{c.symbol ?? c.key}</Ident>
                </span>
                <span className="text-muted-foreground">
                  <span className="line-through">{valueWords(c, c.before)}</span>
                  {" → "}
                  <span className="text-foreground">{valueWords(c, c.after)}</span>
                </span>
              </div>
            ) : c.kind === "text" ? (
              <div key={i} className="flex flex-col gap-1">
                <span>{c.file ? sopName(c.file) : "General instructions"}</span>
                <div className="rounded-lg border bg-card py-2 text-sm">
                  <LineDiffView diff={c.diff} />
                </div>
              </div>
            ) : (
              <div key={i} className="flex flex-col gap-0.5">
                <span>Example cases</span>
                <span className="text-muted-foreground">
                  {[
                    c.added.length && `Added ${c.added.join(", ")}`,
                    c.changed.length && `Changed ${c.changed.join(", ")}`,
                    c.removed.length && `Removed ${c.removed.join(", ")}`,
                  ]
                    .filter(Boolean)
                    .join(". ")}
                </span>
              </div>
            )
          )}
        </div>
      ))}
    </section>
  )
}
