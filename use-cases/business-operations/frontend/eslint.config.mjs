// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import tseslint from "@typescript-eslint/eslint-plugin"
import tsparser from "@typescript-eslint/parser"
import jsxA11y from "eslint-plugin-jsx-a11y"
import reactHooks from "eslint-plugin-react-hooks"
import consoleRules, { RESTRICTED_IMPORTS, RESTRICTED_SYNTAX } from "./eslint/console-rules.mjs"

const eslintConfig = [
  {
    files: ["**/*.ts", "**/*.tsx"],
    languageOptions: {
      parser: tsparser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      "@typescript-eslint": tseslint,
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": "warn",
    },
  },
  // The a11y basics the console already has (labels, roles, no click handlers on divs)
  // and the rules of hooks, kept by lint rather than by review. The classic two hooks
  // rules only: the plugin's React Compiler rules flag the latest-value ref pattern this
  // app uses on purpose, and the app does not run the compiler.
  { files: ["**/*.tsx"], ...jsxA11y.flatConfigs.recommended },
  {
    files: ["**/*.ts", "**/*.tsx"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  },
  // Stock shadcn components, kept as the registry ships them. `Label` is tied to its
  // control by `htmlFor` where it is used, not inside the component; the input group's
  // addon click only moves focus to an input that is reachable by keyboard anyway.
  {
    files: ["src/components/ui/label.tsx", "src/components/ui/input-group.tsx"],
    rules: {
      "jsx-a11y/label-has-associated-control": "off",
      "jsx-a11y/click-events-have-key-events": "off",
      "jsx-a11y/no-noninteractive-element-interactions": "off",
    },
  },
  // The design rules (console refresh plan, Enforcement): pages compose components/ui and
  // components/layout and style nothing themselves. Errors since phase 5.
  {
    files: ["src/**/*.tsx"],
    ignores: [
      "src/components/ui/**",
      "src/components/layout/**",
      "src/**/*.test.tsx",
      "src/test/**",
      "src/mocks/**",
    ],
    plugins: { console: consoleRules },
    rules: {
      "console/banned-classes": "error",
      "no-restricted-syntax": ["error", ...RESTRICTED_SYNTAX],
    },
  },
  {
    files: ["src/**/*.ts", "src/**/*.tsx"],
    ignores: ["src/components/ui/**", "src/**/*.test.ts", "src/**/*.test.tsx", "src/test/**"],
    rules: { "no-restricted-imports": ["error", RESTRICTED_IMPORTS] },
  },
  // Generated from types/*.schema.json (`ops.py types`): an open schema object is an index
  // signature of `any` there, and the fix belongs in the generator, not in this file.
  {
    files: ["src/types/generated-*.ts"],
    rules: { "@typescript-eslint/no-explicit-any": "off" },
  },
  {
    ignores: ["node_modules/**", "build/**", "delete/**", "tmp/**", ".vite/**"],
  },
]

export default eslintConfig
