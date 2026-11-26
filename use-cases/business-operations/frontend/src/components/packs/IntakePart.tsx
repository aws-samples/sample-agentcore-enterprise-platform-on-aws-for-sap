// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Ident } from "@/components/Ident"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Fact, Facts, PartHeader } from "@/components/packs/PartHeader"
import { usePackDocument } from "@/hooks/usePack"
import { humanise, type UseCasePack } from "@/lib/registry"

interface Source {
  label?: string
  kind?: string
  push?: string
  service?: string
  entity?: string
  filter?: string
  short_id?: string
  key?: { field: string }[]
  process_type?: { default?: string }
  field_map?: Record<string, { path?: string }>
}

interface SapService {
  services?: { service: string; entities?: Record<string, { name: string }> }[]
}

const KIND: Record<string, string> = {
  filter: "A record whose own values make it an exception",
  absence: "A record missing a document that should exist",
  aging: "A record older than a window and still open",
}

/**
 * Intake: what becomes a case, in words first and the SAP condition beside it; how cases
 * arrive; the fields a case shows; and what the agent may read and change. Read from
 * `GET /packs/{pack_id}`, the keys the API puts in this part.
 */
export function IntakePart({ pack }: { pack: UseCasePack }) {
  const doc = usePackDocument(pack.id)
  if (doc.isError)
    return (
      <Alert variant="destructive">
        <AlertTitle>Couldn&apos;t read the pack</AlertTitle>
        <AlertDescription>{(doc.error as Error).message}</AlertDescription>
      </Alert>
    )
  if (!doc.data) return <Skeleton className="h-48 w-full" />

  const p = doc.data.pack
  const source = (p.source ?? {}) as Source
  const document_check = (p.case_document_check ?? {}) as Record<string, string>
  const services = ((p.sap_service ?? {}) as SapService).services ?? []
  const triage = source.process_type?.default

  return (
    <div className="flex flex-col gap-6">
      <PartHeader pack={pack} part="intake" />
      <Card>
        <CardHeader>
          <CardTitle>What becomes a case</CardTitle>
          <CardDescription>{source.label ?? pack.label}</CardDescription>
        </CardHeader>
        <CardContent>
          <Facts>
            {source.kind && <Fact label="Kind">{KIND[source.kind] ?? source.kind}</Fact>}
            {source.entity && (
              <Fact label="SAP object">
                <Ident>{source.entity}</Ident>
                {source.service && <Ident className="ml-2">{source.service}</Ident>}
              </Fact>
            )}
            {source.filter && (
              <Fact label="Condition">
                <Ident>{source.filter}</Ident>
              </Fact>
            )}
            {source.key?.length ? (
              <Fact label="One case per">
                <Ident>{source.key.map(k => k.field).join(", ")}</Ident>
              </Fact>
            ) : null}
            {source.short_id && (
              <Fact label="Case id prefix">
                <Ident>{source.short_id}-</Ident>
              </Fact>
            )}
          </Facts>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>How cases arrive</CardTitle>
        </CardHeader>
        <CardContent>
          <Facts>
            {source.push && <Fact label="SAP sends an event">When it raises the exception</Fact>}
            <Fact label="Poll">
              {source.push ? "Regularly, catching any event SAP missed" : "Regularly"}
            </Fact>
            {triage && <Fact label="First step">Triage: {humanise(triage)}</Fact>}
          </Facts>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>What a case shows</CardTitle>
        </CardHeader>
        <CardContent className="px-0">
          <Table className="table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="pl-6">Field</TableHead>
                <TableHead className="w-32">Type</TableHead>
                <TableHead className="pr-6">From SAP</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pack.fields.map(f => (
                <TableRow key={f.attribute} className="h-9">
                  <TableCell className="truncate pl-6">
                    {f.label}
                    {f.panel === false && (
                      <span className="ml-2 text-xs text-muted-foreground">
                        not on the case page
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{f.type}</TableCell>
                  <TableCell className="truncate pr-6 text-muted-foreground">
                    {f.sap_field ? <Ident>{f.sap_field}</Ident> : "Derived"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>What the agent may read and change</CardTitle>
        </CardHeader>
        <CardContent>
          <Facts>
            <Fact label="Changes">
              {Object.keys(document_check).length ? (
                <>
                  Only the case&apos;s own document (
                  <Ident>{Object.keys(document_check).join(", ")}</Ident>)
                </>
              ) : (
                "Nothing in SAP"
              )}
            </Fact>
            {services.map(s => (
              <Fact key={s.service} label="Reads">
                <Ident>
                  {Object.values(s.entities ?? {})
                    .map(e => e.name)
                    .join(", ")}
                </Ident>
                <Ident className="ml-2">{s.service}</Ident>
              </Fact>
            ))}
          </Facts>
        </CardContent>
      </Card>
    </div>
  )
}
