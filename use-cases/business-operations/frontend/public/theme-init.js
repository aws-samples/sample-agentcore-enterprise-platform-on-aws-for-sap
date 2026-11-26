// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Blocking on purpose: React mounts after first paint, so setting .dark from a component
// flashes the light theme first. Mirrors src/lib/useTheme.ts — change both together.
//
// This lives in public/ rather than inline in index.html so the distribution's CSP can be
// `script-src 'self'`. Inline would need a sha256 in the CDK response-headers policy, which
// is a constant in a second language that silently blocks the theme when someone edits this
// script and not the hash.
try {
  var p = localStorage.getItem("ui.theme")
  if (
    p === "dark" ||
    ((!p || p === "system") && matchMedia("(prefers-color-scheme: dark)").matches)
  )
    document.documentElement.classList.add("dark")
} catch (e) {}
