// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The console's design rules, as lint: pages compose the components in `components/ui`
 * and `components/layout` and set no styling of their own. See frontend/REFERENCE.md.
 *
 * One rule, `banned-classes`, reads every class string a page writes: a `className`
 * attribute, and the arguments to `cn()` and `cva()`. Each banned pattern names what to
 * use instead, so a warning is also the fix.
 */

const PALETTE =
  "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose"
const COLOR_UTILS = "bg|text|border|ring|fill|stroke|from|to|via|outline|divide|decoration|accent|caret|shadow"

/** [pattern over one class token with its variants removed, message] */
export const BANNED = [
  [/^-?space-[xy]-/, "space-x/y: use gap on a flex or grid parent"],
  [/^-?[a-z0-9-]+-\[.+\]$/, "arbitrary values: use a value from the scale or a token"],
  [/^\[.+\]$/, "arbitrary properties: use a utility or a component"],
  [/^text-[23]xs$/, "text-2xs and text-3xs: the smallest size is text-xs"],
  [/^h-(6|7|10|11)$/, "h-6, h-7, h-10, h-11: use a component size (Button sm or default)"],
  [/^rounded(-[trblxyse]{1,2})?(-(none|xs|sm|xl|2xl|3xl|4xl))?$/, "radius: rounded-md, rounded-lg or rounded-full only"],
  [/^-?z-/, "z-index: only components in ui/ set layers"],
  [/^shadow(-|$)/, "shadows: the shell is flat; overlays in ui/ carry their own"],
  [new RegExp(`^(${COLOR_UTILS})-[a-z-]+/\\d+$`), "alpha modifiers: use a token"],
  [new RegExp(`^(${COLOR_UTILS})-((${PALETTE})-\\d{2,3}|white|black)$`), "raw palette colours: use a token"],
]

/** Variant prefixes (`hover:`, `md:`, `[&>svg]:`) stripped, bracket-aware. */
function bare(token) {
  return token.replace(/^!/, "").replace(/^((\[[^\]]*\]|[^:[\]]+):)+/, "")
}

export function findBanned(classes) {
  const hits = []
  for (const token of classes.split(/\s+/).filter(Boolean)) {
    if (/(^|:)dark:/.test(token)) {
      hits.push({ token, msg: "dark: variants: the tokens already switch with the theme" })
      continue
    }
    const b = bare(token)
    const hit = BANNED.find(([re]) => re.test(b))
    if (hit) hits.push({ token, msg: hit[1] })
  }
  return hits
}

const bannedClasses = {
  meta: {
    type: "suggestion",
    docs: { description: "Ban class names the design system replaces with components and tokens" },
    schema: [],
  },
  create(context) {
    function check(node, text) {
      for (const { token, msg } of findBanned(text)) {
        context.report({ node, message: `"${token}": ${msg}` })
      }
    }
    function visit(node) {
      if (!node) return
      if (node.type === "Literal" && typeof node.value === "string") check(node, node.value)
      else if (node.type === "TemplateLiteral") node.quasis.forEach(q => check(q, q.value.cooked ?? ""))
      else if (node.type === "ConditionalExpression") {
        visit(node.consequent)
        visit(node.alternate)
      } else if (node.type === "LogicalExpression") visit(node.right)
      else if (node.type === "ArrayExpression") node.elements.forEach(visit)
      else if (node.type === "ObjectExpression")
        node.properties.forEach(p => {
          if (p.type === "Property") {
            visit(p.value)
            if (p.key.type === "Literal") visit(p.key)
          }
        })
    }
    return {
      JSXAttribute(node) {
        if (node.name.name !== "className" || !node.value) return
        if (node.value.type === "Literal") visit(node.value)
        else if (node.value.type === "JSXExpressionContainer") visit(node.value.expression)
      },
      CallExpression(node) {
        if (node.callee.type === "Identifier" && ["cn", "cva", "clsx"].includes(node.callee.name)) {
          node.arguments.forEach(visit)
        }
      },
    }
  },
}

export default { rules: { "banned-classes": bannedClasses } }

/** Raw HTML elements that a component in ui/ replaces, with the replacement. */
const ELEMENTS = {
  button: "Button",
  input: "Input",
  textarea: "Textarea",
  select: "Select",
  table: "Table",
  details: "Collapsible",
  hr: "Separator",
  kbd: "Kbd",
}

export const RESTRICTED_SYNTAX = [
  ...Object.entries(ELEMENTS).map(([el, comp]) => ({
    selector: `JSXOpeningElement[name.name='${el}']`,
    message: `Raw <${el}>: use ${comp} from @/components/ui`,
  })),
  {
    selector: "JSXOpeningElement[name.name=/^[a-z]/][name.name!='iframe'] > JSXAttribute[name.name='title']",
    message: "title attribute: keyboard and touch users never see it; use Tooltip",
  },
  {
    selector: "JSXAttribute[name.name='strokeWidth']",
    message: "Icon strokeWidth: icons use the stock stroke",
  },
  {
    selector: "JSXAttribute[name.name='size'][value.type='JSXExpressionContainer']",
    message: "Icon size: icons take their size from the component they sit in",
  },
  {
    selector: "JSXOpeningElement[name.name='Button'] > JSXAttribute[name.name='size'][value.value='lg']",
    message: "Button size lg: use default or sm",
  },
]

export const RESTRICTED_IMPORTS = {
  paths: [{ name: "allotment", message: "Split panes are gone: use the page templates in components/layout" }],
  patterns: [
    {
      group: ["@radix-ui/*", "radix-ui", "@base-ui/*", "@base-ui-components/*"],
      message: "Primitives are wrapped in components/ui; import the component from @/components/ui",
    },
  ],
}
