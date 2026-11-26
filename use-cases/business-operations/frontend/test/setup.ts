// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import "@testing-library/jest-dom"

// jsdom 23 has no `<dialog>` modal API. These two cover what the app calls: the open
// attribute, and the `close` event.
const dialog = HTMLDialogElement.prototype
dialog.showModal ??= function () {
  this.setAttribute("open", "")
}
dialog.close ??= function () {
  if (this.hasAttribute("open")) {
    this.removeAttribute("open")
    this.dispatchEvent(new Event("close"))
  }
}

// jsdom has no matchMedia; the sidebar's mobile check and the theme read it. A wide,
// light, no-preference screen, unless a test stubs its own.
if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

// jsdom has no PointerEvent; Base UI's radio dispatches one on click.
if (typeof window !== "undefined" && !window.PointerEvent) {
  class PointerEvent extends MouseEvent {
    pointerType: string
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init)
      this.pointerType = init.pointerType ?? "mouse"
    }
  }
  Object.defineProperty(window, "PointerEvent", { writable: true, value: PointerEvent })
}
