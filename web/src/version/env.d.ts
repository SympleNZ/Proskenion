/* The build's own version, injected by `vite.config.ts`'s `define` from
 * pyproject.toml's `[project] version` — see `buildVersion.ts`. */
declare const __APP_VERSION__: string;
/* The git short hash and build date this bundle was built from, or
 * "unknown · <date>" where git is unavailable — see `buildVersion.ts`. */
declare const __BUILD_ID__: string;
