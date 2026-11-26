// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react"
import ReactDOM from "react-dom/client"
import App from "./App"
// Self-hosted faces: Vite emits the woff2 into /assets, so the CSP names no font origin.
import "@fontsource-variable/geist/wght.css"
import "@fontsource-variable/geist/wght-italic.css"
import "@fontsource-variable/geist-mono/wght.css"
import "./styles/globals.css"

async function boot() {
  // Mock mode (`npm run dev:mock`): an in-browser API and a fake sign-in. Both conditions
  // are compile-time in a build, where this branch and its import are removed.
  if (import.meta.env.DEV && import.meta.env.VITE_MOCK === "1") {
    const { startMock } = await import("./mocks/start")
    await startMock()
  }
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  )
}

void boot()
