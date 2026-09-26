// The whole lint toolchain — ESLint, the plugins and a lint-only TypeScript
// 6.x — lives in tools/lint-toolchain (spec §5.1: typescript-eslint gates on
// TypeScript 7.1). `npm run lint` runs that ESLint from here so this config is
// still discovered at the project root.
import { globals, js, reactHooks, reactRefresh, tseslint } from "./tools/lint-toolchain/index.js";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "tools", "public"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended, reactHooks.configs.flat.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      globals: { ...globals.browser, ...globals.node },
    },
    plugins: { "react-refresh": reactRefresh },
    rules: {
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    },
  },
  {
    // Browser-local date/time formatting, banned everywhere except the one
    // module that exists to prevent it (spec §4.9, §21.7): the appliance's
    // zone is Pacific/Auckland, always, whatever zone the viewing device is
    // in, and `toLocaleDateString`/`getHours` and their relatives read the
    // *runtime's* zone. `web/src/lib/time.ts` is the only file allowed to
    // call them; everything else formats a date or a time through it.
    //
    // This matches by property name rather than by the receiver's type —
    // there is no type-aware linting configured here (no
    // `tseslint.configs.recommendedTypeChecked`) — so it would flag an
    // unrelated object that happened to expose a same-named method (a
    // `getDate()` on something that is not a `Date`, say). Nothing in this
    // codebase does that today (checked by grep across `src/`), and the
    // property names below are otherwise distinctive enough that a false
    // positive would be surprising rather than routine.
    files: ["**/*.{ts,tsx}"],
    ignores: ["src/lib/time.ts"],
    rules: {
      "no-restricted-properties": [
        "error",
        ...[
          "toLocaleDateString",
          "toLocaleTimeString",
          "toLocaleString",
          "getHours",
          "getMinutes",
          "getSeconds",
          "getDate",
          "getMonth",
          "getDay",
          "getFullYear",
          "setHours",
        ].map((property) => ({
          property,
          message: `Format dates and times through web/src/lib/time.ts, which fixes the appliance's Pacific/Auckland zone (spec §4.9, §21.7) regardless of the viewing device's own zone.`,
        })),
      ],
    },
  },
);
