// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useRef, useState } from "react"
import { Outlet, useOutletContext } from "react-router"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"
import { Toaster } from "@/components/ui/sonner"
import { AppSidebar } from "@/components/layout/AppSidebar"
import { AppHeader } from "@/components/layout/AppHeader"
import { CommandMenu } from "@/components/layout/CommandMenu"
import { AskBar, AssistantPanel } from "@/components/layout/Assistant"
import { SandboxBand } from "@/components/SandboxMarker"
import { StopBand } from "@/components/EmergencyStop"
import { useAgentChat, type ShellContext } from "@/hooks/useAgentChat"
import { useStoredState } from "@/hooks/useStoredState"
import { getTranscript } from "@/hooks/transcript"

/** Expanded from 1440 px, icons below it, unless the person chose (plan, Layout). */
const sidebarDefault = () => typeof window === "undefined" || window.innerWidth >= 1440

/**
 * Root layout: sidebar, header band, the routed page, and the assistant.
 *
 * The conversation is owned here rather than by a page because a run has to survive
 * navigation. `<Outlet context>` hands it to the routed page; `useAssistant()` reads it.
 *
 * The assistant panel opens only when something you asked lands in it (decision 20): Enter
 * in the ask bar, "Ask the assistant about…" in ⌘K, or a page asking for a briefing. It
 * never opens on its own for a page load, a background run or a rehearsal (whose steps show
 * on the Rehearse page), and it closes only when you close it.
 */
export function AppShell() {
  const chat = useAgentChat()
  const [sidebarOpen, setSidebarOpen] = useStoredState("ui.sidebar.open", sidebarDefault)
  const [assistantOpen, setAssistantOpen] = useStoredState("ui.assistant.open", false)
  const [searchOpen, setSearchOpen] = useState(false)
  const askRef = useRef<HTMLInputElement>(null)
  const panelOpen = assistantOpen

  const openAssistant = useCallback(() => setAssistantOpen(true), [setAssistantOpen])
  const ask = useCallback(
    (question: string) => {
      setAssistantOpen(true)
      void chat.send(question)
    },
    [chat, setAssistantOpen]
  )

  // ⌘J: close the panel; or reopen the conversation about what is on screen; or, with none
  // yet, put the cursor in the ask bar. ⌘K: search.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return
      const key = e.key.toLowerCase()
      if (key === "k") {
        e.preventDefault()
        setSearchOpen(o => !o)
      } else if (key === "j") {
        e.preventDefault()
        if (panelOpen) setAssistantOpen(false)
        else if (getTranscript(chat.slotKey).length) setAssistantOpen(true)
        else askRef.current?.focus()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [panelOpen, chat.slotKey, setAssistantOpen])

  return (
    <SidebarProvider open={sidebarOpen} onOpenChange={setSidebarOpen}>
      {/* Ahead of the sidebar, so the first Tab from page load skips a nav that repeats on
          every route. */}
      <a
        href="#main"
        className="skip-link"
        onClick={e => {
          // The router owns the URL, so move focus here rather than through the hash.
          e.preventDefault()
          document.getElementById("main")?.focus()
        }}
      >
        Skip to content
      </a>
      <AppSidebar />
      <SidebarInset className="h-svh min-w-0 overflow-hidden">
        <StopBand />
        <SandboxBand />
        <AppHeader onSearch={() => setSearchOpen(true)} />
        <div className="relative flex min-h-0 flex-1">
          {/* SidebarInset is the page's <main>; this is the skip link's target inside it. */}
          <div
            id="main"
            tabIndex={-1}
            className="relative min-w-0 flex-1 overflow-hidden outline-none"
          >
            <Outlet context={{ chat, ask, openAssistant } satisfies ShellContext} />
            {!panelOpen && <AskBar chat={chat} onOpen={openAssistant} inputRef={askRef} />}
          </div>
          {panelOpen && <AssistantPanel chat={chat} onClose={() => setAssistantOpen(false)} />}
        </div>
      </SidebarInset>
      <CommandMenu open={searchOpen} onOpenChange={setSearchOpen} onAsk={ask} />
      <Toaster position="bottom-right" />
    </SidebarProvider>
  )
}

/**
 * Frame for the pages that scroll as one column under the header. Forwards the outlet
 * context: a bare `<Outlet />` would publish `undefined` and the pages below would lose the
 * assistant.
 */
export function PageFrame() {
  const context = useOutletContext<ShellContext>()
  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden">
      <Outlet context={context} />
    </div>
  )
}
