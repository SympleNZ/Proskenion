/*
 * The Updates screen (spec §21.24 *Updates*, §14.1, §6.11, Q8, contracts §3,
 * §5), against the real application — never mocked at the browser network
 * layer, the same standard every other spec in this directory holds to: a
 * real key pair and a real package, built and signed exactly as
 * `docs/plans/phase-6.md`'s Q8 describes, streamed through the real drop
 * zone to a live `POST /system/update`, and refused for a real reason by the
 * real verifier (`proskenion/core/packages.py`).
 *
 * What this file does NOT attempt, and why:
 *
 *   - **A successful review, an apply, and reconnection.** §6.11 fixes trust
 *     anchors at `/usr/local/share/auditorium/trusted-keys/` on the golden
 *     image — deliberately: an application-level override would let a
 *     package ship its own anchor and self-authorise every later update, the
 *     exact thing that path is designed to prevent. This harness starts the
 *     real application with no such directory and no way to give it one, so
 *     no key it generates can ever verify here, by design rather than by
 *     omission. `docs/plans/phase-6.md`'s own line is "Proved:
 *     off-device" for exactly this reason — a real signed apply is proved on
 *     the bench (`tests/hil/phase6/`), against a real trust anchor
 *     and a real `auditorium-helper`.
 *   - **`Apply now` reaching a restart.** `apply-update` and `restart-core`
 *     go through `auditorium-helper`, a root systemd unit
 *     (`contracts/§2`) — the same category of gap `system-screens.spec.ts`
 *     already documents for the Network screen's own reconnection flow, for
 *     the same reason: nothing here runs as root, and nothing here is
 *     systemd.
 *
 * Everything else about this screen — every rejection's exact wording, the
 * apply choices, the quiet-moment conditions, roll back, the OS section in
 * each state, restart and reboot, and the banners — is proved in Vitest
 * against a mocked API (`web/src/admin/updates`), which is the faster and
 * more exhaustive place for it. What a mock cannot prove, and what this file
 * is for, is that a real `.aupkg` built by `tools/package.py` — the same
 * tool and the same verifier code the appliance itself runs — streams
 * through the browser to the live server and comes back refused for the
 * real reason, in the real sentence §21.24 promises.
 */
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { test, UV } from "./fixtures/appliance";
import { REPO_ROOT } from "./fixtures/build-frontend";
import { commission } from "./fixtures/rig";

const { expect } = test;

function run(argv: string[]): void {
  execFileSync(UV, argv, { cwd: REPO_ROOT, stdio: "pipe" });
}

/**
 * `tools/package.py keygen`, `build` and `sign` — the real workflow Q8
 * describes, producing a genuinely Ed25519-signed `.aupkg`. The payload's
 * content is a placeholder: §6.11's checks this harness can reach (layout,
 * signature, member digests) run before anything is extracted, and never
 * look inside it.
 */
async function buildSignedAppPackage(
  workDir: string,
  options: { version: string; description: string; changes: readonly string[] },
): Promise<string> {
  const source = path.join(workDir, "payload-src");
  await mkdir(source, { recursive: true });
  await writeFile(path.join(source, "app-marker.txt"), "e2e placeholder payload — never extracted here\n", "utf8");

  const keysDir = path.join(workDir, "keys");
  run(["run", "python", "tools/package.py", "keygen", "--name", "e2e", "--out-dir", keysDir, "--no-passphrase"]);

  const packagePath = path.join(workDir, `app-${options.version}.tar`);
  run([
    "run",
    "python",
    "tools/package.py",
    "build",
    "--type",
    "app",
    "--version",
    options.version,
    "--source",
    source,
    "--output",
    packagePath,
    "--description",
    options.description,
    ...options.changes.flatMap((change) => ["--change", change]),
  ]);

  run(["run", "python", "tools/package.py", "sign", "--package", packagePath, "--key", path.join(keysDir, "e2e.key"), "--no-passphrase"]);

  return packagePath;
}

test.describe("Updates screen — a real signed package meets the live verifier (§6.11, Q8, §21.24)", () => {
  test("streams a real .aupkg through the drop zone and is refused for a real reason", async ({ page }) => {
    await commission(page.request);
    const workDir = await mkdtemp(path.join(tmpdir(), "proskenion-e2e-pkg-"));
    try {
      const packagePath = await buildSignedAppPackage(workDir, {
        version: "v9.9.9",
        description: "An end-to-end test package",
        changes: ["Nothing real — this package exists only to reach the live verifier"],
      });

      await page.goto("/admin/updates");
      await expect(page.getByRole("heading", { name: "Updates", level: 1 })).toBeVisible();

      await page.locator('input[type="file"]').setInputFiles(packagePath);

      // A real, otherwise-untrusted signature: this harness's appliance has
      // no trust anchors at all (see the module docstring), so even a
      // correctly signed package is refused — but only after the live
      // server has genuinely parsed the manifest, checked every member
      // against it, and looked for an anchor to check the signature
      // against. That is real verification work, not a canned response.
      await expect(page.getByText("This package was refused")).toBeVisible();
      await expect(
        page.getByText("No package signing key is installed, so no package can be verified."),
      ).toBeVisible();

      // The drop zone is usable again — nothing about the refusal wedges the screen.
      await expect(page.getByRole("button", { name: "Upload an update package" })).toBeVisible();
    } finally {
      await rm(workDir, { recursive: true, force: true });
    }
  });

  test("a file that is not a package at all reads the same sentence §21.24 promises", async ({ page }) => {
    await commission(page.request);
    const workDir = await mkdtemp(path.join(tmpdir(), "proskenion-e2e-pkg-"));
    try {
      const garbage = path.join(workDir, "not-a-package.aupkg");
      await writeFile(garbage, "this is not a tar file at all\n".repeat(200), "utf8");

      await page.goto("/admin/updates");
      await page.locator('input[type="file"]').setInputFiles(garbage);

      await expect(page.getByText("This package was refused")).toBeVisible();
      await expect(page.getByText("This file is not a package, or it did not arrive intact.")).toBeVisible();
    } finally {
      await rm(workDir, { recursive: true, force: true });
    }
  });
});
