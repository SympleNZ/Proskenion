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
);
