// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Ident } from "@/components/Ident"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Fact, Facts, PartHeader } from "@/components/packs/PartHeader"
import { usePackDocument } from "@/hooks/usePack"
import { humanise, type UseCasePack } from "@/lib/registry"

/** `<target>___<tool>` → the tool and the Gateway target it sits on. */
function splitTool(id: string): { tool: string; target: string } {
  const at = id.lastIndexOf("___")
  return at < 0 ? { tool: id, target: "" } : { tool: id.slice(at + 3), target: id.slice(0, at) }
}

/** Agent configuration: the model tier, the turn budget, and the Gateway tools in plain words. */
export function AgentPart({ pack }: { pack: UseCasePack }) {
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
  const tools = Array.isArray(p.gateway_tools) ? (p.gateway_tools as string[]) : []
  const verifier = (p.verifier ?? null) as { enabled?: boolean; model_tier?: string } | null

  return (
    <div className="flex flex-col gap-6">
      <PartHeader pack={pack} part="agent" />
      <Card>
        <CardHeader>
          <CardTitle>Model</CardTitle>
        </CardHeader>
        <CardContent>
          <Facts>
            <Fact label="Model tier">
              {p.model_tier ? humanise(String(p.model_tier)) : "Default"}
            </Fact>
            <Fact label="Turn budget">
              {p.max_turns ? `${String(p.max_turns)} turns per run` : "Default"}
            </Fact>
            {verifier && (
              <Fact label="Verifier model">
                {verifier.model_tier ? humanise(verifier.model_tier) : "Default"}
                {!verifier.enabled && (
                  <span className="text-muted-foreground"> (verifier off)</span>
                )}
              </Fact>
            )}
          </Facts>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Tools</CardTitle>
          <CardDescription>
            The Gateway tools the agent may call. The Gateway checks every call against the
            controls.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="flex flex-col gap-1.5 text-sm">
            {tools.map(id => {
              const { tool, target } = splitTool(id)
              return (
                <li key={id} className="flex items-baseline gap-2">
                  <span>{humanise(tool)}</span>
                  <Ident className="truncate">{target}</Ident>
                </li>
              )
            })}
          </ul>
        </CardContent>
      </Card>
    </div>
  )
}
