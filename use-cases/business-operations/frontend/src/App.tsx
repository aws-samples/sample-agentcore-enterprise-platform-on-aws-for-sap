// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { RouterProvider, createBrowserRouter } from "react-router"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { AuthProvider } from "@/components/auth/AuthProvider"
import { TooltipProvider } from "@/components/ui/tooltip"
import AppRoutes from "./routes"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
    },
  },
})

/**
 * A data router with one catch-all route, so the app's own `<Routes>` stay as they are and a
 * page can block navigation while it holds unsaved work (`useBlocker`, the procedure editor).
 */
const router = createBrowserRouter([
  {
    path: "*",
    element: (
      <AuthProvider>
        <TooltipProvider>
          <AppRoutes />
        </TooltipProvider>
      </AuthProvider>
    ),
  },
])

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
}
