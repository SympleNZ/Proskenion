// Re-exports the lint toolchain from this nested install so every package that
// does `require("typescript")` resolves the lint-only TypeScript 6.x pinned here
// (spec §5.1) rather than the TypeScript 7 native compiler at the root, which
// has no programmatic API for typescript-eslint to use yet.
export { default as js } from "@eslint/js";
export { default as globals } from "globals";
export { default as reactHooks } from "eslint-plugin-react-hooks";
export { default as reactRefresh } from "eslint-plugin-react-refresh";
export { default as tseslint } from "typescript-eslint";
