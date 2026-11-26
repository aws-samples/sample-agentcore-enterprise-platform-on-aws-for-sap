// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Suspense, lazy } from "react"
import { Navigate, Routes, Route, useLocation } from "react-router"
import { AppShell, PageFrame } from "./AppShell"
import { useDemoFeatures } from "@/hooks/useDemoEnabled"

const CasesPage = lazy(() => import("./CasesPage"))
const CasePage = lazy(() => import("./CasePage"))
const TicketsPage = lazy(() => import("./TicketsPage"))
const PacksIndexPage = lazy(() => import("./PacksIndexPage"))
const PackPage = lazy(() => import("./PackPage"))
const ProcedurePage = lazy(() => import("./ProcedurePage"))
const RehearsePage = lazy(() => import("./RehearsePage"))
const DeploymentSettingsPage = lazy(() => import("./DeploymentSettingsPage"))

/** `/workspace?case=x`, printed in the docs and in ticket links, keeps working. */
function WorkspaceRedirect() {
  const { search } = useLocation()
  return <Navigate to={`/cases${search}`} replace />
}

/** A path under a pack this console does not have opens the pack. */
function PackRedirect() {
  const { pathname } = useLocation()
  const [, , packId] = pathname.split("/")
  return <Navigate to={`/packs/${packId}`} replace />
}

/** Routes (plan, Information architecture). */
export default function AppRoutes() {
  const { ticketing, known } = useDemoFeatures()
  const ticketingPending = known === false
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<Navigate to="/cases" replace />} />
          <Route path="/workspace" element={<WorkspaceRedirect />} />
          <Route element={<PageFrame />}>
            <Route path="/cases" element={<CasesPage />} />
            <Route path="/cases/:caseId" element={<CasePage />} />
            {(ticketing || ticketingPending) && (
              <Route path="/tickets" element={ticketing ? <TicketsPage /> : <RouteFallback />} />
            )}
            <Route path="/packs" element={<PacksIndexPage />} />
            <Route path="/packs/:packId" element={<PackPage />} />
            <Route path="/packs/:packId/procedures/:processType" element={<ProcedurePage />} />
            <Route path="/packs/:packId/instructions" element={<ProcedurePage instructions />} />
            <Route path="/packs/:packId/rehearse" element={<RehearsePage />} />
            <Route path="/packs/:packId/*" element={<PackRedirect />} />
            <Route path="/deployment" element={<DeploymentSettingsPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/cases" replace />} />
        </Route>
      </Routes>
    </Suspense>
  )
}

function RouteFallback() {
  return (
    <div className="flex h-full items-center justify-center p-8" role="status" aria-live="polite">
      <span className="sr-only">Loading page</span>
    </div>
  )
}
