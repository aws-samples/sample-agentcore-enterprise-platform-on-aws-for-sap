// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react"
import { useAuth } from "react-oidc-context"
import { useQueryClient } from "@tanstack/react-query"
import { ChevronsUpDown, FlaskConical, Info, LogOut, SunMoon, Users } from "lucide-react"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { SidebarMenu, SidebarMenuButton, SidebarMenuItem } from "@/components/ui/sidebar"
import { useBrand } from "@/hooks/useBrand"
import { useMarkers } from "@/components/SandboxMarker"
import { useTheme, type ThemePref } from "@/hooks/useTheme"
import { operatorName } from "@/lib/identity"
import { clearOperatorContext } from "@/lib/signOut"
import { getConfig } from "@/lib/config"

function initials(name: string): string {
  return name
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(w => w[0]!.toUpperCase())
    .join("")
}

/**
 * The account row at the foot of the sidebar and its menu: who you are and your groups,
 * the theme, About, and Sign out (with a confirm, since it ends the session).
 */
export function NavUser() {
  const brand = useBrand()
  const auth = useAuth()
  const queryClient = useQueryClient()
  const { pref, choose } = useTheme()
  const [markers, setMarkers] = useMarkers()
  const [about, setAbout] = useState(false)
  const [confirmOut, setConfirmOut] = useState(false)

  const profile = auth.user?.profile
  const name = operatorName(profile) ?? "Signed in"
  const email = typeof profile?.email === "string" ? profile.email : ""
  const groups = Array.isArray(profile?.["cognito:groups"])
    ? (profile["cognito:groups"] as string[])
    : []

  const signOut = async () => {
    const { client_id, redirect_uri } = await getConfig()
    // Wipe before navigating: the redirect can win the race against React's re-render.
    clearOperatorContext(queryClient)
    await auth.signoutRedirect({ extraQueryParams: { client_id, logout_uri: redirect_uri } })
  }

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger render={<SidebarMenuButton size="lg" />}>
            <span className="flex size-8 flex-none items-center justify-center rounded-lg bg-muted text-xs font-semibold">
              {initials(name)}
            </span>
            <span className="grid flex-1 text-left leading-tight">
              <span className="truncate text-sm font-medium">{name}</span>
              {email && <span className="truncate text-xs text-muted-foreground">{email}</span>}
            </span>
            <ChevronsUpDown className="ml-auto text-muted-foreground" />
          </DropdownMenuTrigger>
          <DropdownMenuContent side="right" align="end" className="w-64">
            <DropdownMenuGroup>
              <DropdownMenuLabel className="font-normal">
                <span className="block truncate font-medium text-foreground">{name}</span>
                {email && (
                  <span className="block truncate text-xs text-muted-foreground">{email}</span>
                )}
              </DropdownMenuLabel>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem disabled>
                <Users />
                <span className="truncate">{groups.length ? groups.join(", ") : "No groups"}</span>
              </DropdownMenuItem>
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>
                  <SunMoon />
                  Appearance
                </DropdownMenuSubTrigger>
                <DropdownMenuSubContent>
                  <DropdownMenuRadioGroup value={pref} onValueChange={v => choose(v as ThemePref)}>
                    <DropdownMenuRadioItem value="system">System</DropdownMenuRadioItem>
                    <DropdownMenuRadioItem value="light">Light</DropdownMenuRadioItem>
                    <DropdownMenuRadioItem value="dark">Dark</DropdownMenuRadioItem>
                  </DropdownMenuRadioGroup>
                </DropdownMenuSubContent>
              </DropdownMenuSub>
              {/* Hiding the markers only ever makes a sandbox look like production (decision 22). */}
              <DropdownMenuCheckboxItem checked={markers} onCheckedChange={on => setMarkers(on)}>
                <FlaskConical />
                Sandbox markers
              </DropdownMenuCheckboxItem>
              <DropdownMenuItem onClick={() => setAbout(true)}>
                <Info />
                About
              </DropdownMenuItem>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem onClick={() => setConfirmOut(true)}>
                <LogOut />
                Sign out
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>

        <Dialog open={about} onOpenChange={setAbout}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{brand.name}</DialogTitle>
              <DialogDescription>
                {brand.subtitle}, release {brand.release}.
              </DialogDescription>
            </DialogHeader>
            <p className="text-sm text-muted-foreground">
              This is sample code, not for production use as is. Review the production readiness
              checklist before you rely on it.
            </p>
          </DialogContent>
        </Dialog>

        <AlertDialog open={confirmOut} onOpenChange={setConfirmOut}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Sign out?</AlertDialogTitle>
              <AlertDialogDescription>
                Any conversation with the assistant in this window is cleared.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={() => void signOut()}>Sign out</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}
