/*
 * The version this bundle was built as (spec §16 "Service worker"). Compared
 * against `/health`'s `version` by `versionCheck.ts` to detect that a newer
 * build is running on the server than the one on screen — the stale-page
 * symptom seen on the CM5, 25 Sep: the admin's browser kept the old Backup
 * page's JS after v0.1.3 installed, with no prompt to reload.
 *
 * `__APP_VERSION__` is a `vite.config.ts` `define`, read at build time from
 * pyproject.toml's `[project] version` — the same value `proskenion.__version__`
 * reports, so there is exactly one number to bump for a release, not two
 * that can drift apart.
 */
export const BUILD_VERSION: string = __APP_VERSION__;

/*
 * The git short hash and build date this bundle was built from (spec §21.24
 * *Help*: "version and build information"), shown alongside `BUILD_VERSION`
 * in the `?` sheet — a version number several rebuilds can share, but the
 * build ID pins down exactly which one is running, which is what a school IT
 * contact needs to read out over the phone. `__BUILD_ID__` is another
 * `vite.config.ts` `define`; where `git` or `.git` is unavailable at build
 * time (`build_package.sh`'s clean-worktree builds), it reads "unknown"
 * rather than failing the build.
 */
export const BUILD_ID: string = __BUILD_ID__;
