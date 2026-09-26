/*
 * The import wizard (§7.1 *Bulk import*, §21.19 *Import wizard*). `/knx/import`
 * is multipart, sent with a raw `fetch` (see `api.ts`'s module docstring), so
 * these tests stub `global.fetch` directly rather than mocking `@/api/client`.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { ImportWizard } from "./ImportWizard";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

function file(name: string, content: string): File {
  return new File([content], name, { type: "text/csv" });
}

async function chooseFile(target: File) {
  const input = screen.getByLabelText("Choose a file to import", { selector: "input" });
  await waitFor(() => fireEvent.change(input, { target: { files: [target] } }));
}

describe("ImportWizard", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("maps a generic CSV's columns before it can be previewed", async () => {
    renderWithProviders(<ImportWizard open onOpenChange={() => {}} />);
    await chooseFile(file("stage.csv", "GA,Title,Kind\n1/0/1,Stage Lights,1.001\n"));

    expect(await screen.findByText(/does not look like an ETS export/)).toBeInTheDocument();
    const continueButton = screen.getByRole("button", { name: "Continue" });
    expect(continueButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText("GA maps to"), { target: { value: "group_address" } });
    fireEvent.change(screen.getByLabelText("Kind maps to"), { target: { value: "dpt" } });
    expect(continueButton).toBeEnabled();

    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        token: "t1",
        format: "generic",
        filename: "stage.csv",
        row_count: 1,
        columns: ["GA", "Title", "Kind"],
        importable_count: 1,
        duplicate_count: 0,
        rows: [
          {
            row_number: 1,
            group_address: "1/0/1",
            name: "Stage Lights",
            description: null,
            dpt: "1.001",
            importable: true,
            existing_id: null,
            existing_name: null,
            warnings: [],
          },
        ],
      }),
    );
    fireEvent.click(continueButton);

    await screen.findByText("stage.csv · 1 row · 1 importable");
    const call = fetchMock.mock.calls[0]!;
    expect(call[0]).toBe("/api/v1/knx/import");
    const sentForm = call[1].body as FormData;
    expect(sentForm.get("step")).toBe("preview");
    expect(JSON.parse(sentForm.get("mapping") as string)).toEqual({ GA: "group_address", Kind: "dpt" });
  });

  it("shows a malformed or unsupported row's warning inline, never dropping it", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, {
        token: "t2",
        format: "ets_csv",
        filename: "export.csv",
        row_count: 1,
        columns: ["Main", "Middle", "Sub", "Name", "Description", "Data Type"],
        importable_count: 1,
        duplicate_count: 0,
        rows: [
          {
            row_number: 43,
            group_address: "1/0/43",
            name: "Something",
            description: null,
            dpt: "7.600",
            importable: true,
            existing_id: null,
            existing_name: null,
            warnings: [{ field: "dpt", code: "unsupported_dpt", message: 'DPT "7.600" is not supported — will import as unmapped' }],
          },
        ],
      }),
    );

    renderWithProviders(<ImportWizard open onOpenChange={() => {}} />);
    await chooseFile(file("export.csv", "Main,Middle,Sub,Name,Description,Data Type\n1,0,43,Something,,DPT-7.600\n"));

    expect(await screen.findByText('DPT "7.600" is not supported — will import as unmapped')).toBeInTheDocument();
    expect(screen.getByText("1/0/43")).toBeInTheDocument();
  });

  it("shows the server's own message when a .esf file is offered", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(422, {
        error: {
          code: "validation_failed",
          message:
            "The ETS 3/4 .esf format is not supported. Export the project from ETS 5 or 6 as CSV, TSV or group address XML instead.",
          detail: { reason: "esf_not_supported" },
        },
      }),
    );

    renderWithProviders(<ImportWizard open onOpenChange={() => {}} />);
    await chooseFile(file("old_project.esf", "not really esf content"));

    expect(
      await screen.findByText(
        "The ETS 3/4 .esf format is not supported. Export the project from ETS 5 or 6 as CSV, TSV or group address XML instead.",
      ),
    ).toBeInTheDocument();
    // Refused back to the upload step — nothing was accepted.
    expect(screen.getByRole("button", { name: "Choose a file" })).toBeInTheDocument();
  });

  it("asks for the file again when the preview token has expired at confirm", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(200, {
          token: "expired-token",
          format: "ets_csv",
          filename: "export.csv",
          row_count: 1,
          columns: [],
          importable_count: 1,
          duplicate_count: 0,
          rows: [
            {
              row_number: 1,
              group_address: "1/0/1",
              name: "Stage",
              description: null,
              dpt: "1.001",
              importable: true,
              existing_id: null,
              existing_name: null,
              warnings: [],
            },
          ],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(404, {
          error: {
            code: "not_found",
            message: "This import preview has expired or was already confirmed; upload the file again",
          },
        }),
      );

    renderWithProviders(<ImportWizard open onOpenChange={() => {}} />);
    await chooseFile(file("export.csv", "Main,Middle,Sub,Name,Description,Data Type\n1,0,1,Stage,,1.001\n"));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Import \d+ address/ }));

    expect(
      await screen.findByText(
        "This import preview has expired or was already confirmed; upload the file again Nothing was imported.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose a file" })).toBeInTheDocument();
  });

  it("shows added, updated and skipped once the import is confirmed", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse(200, {
          token: "t3",
          format: "ets_csv",
          filename: "export.csv",
          row_count: 2,
          columns: [],
          importable_count: 2,
          duplicate_count: 1,
          rows: [
            {
              row_number: 1,
              group_address: "1/0/1",
              name: "Stage",
              description: null,
              dpt: "1.001",
              importable: true,
              existing_id: 9,
              existing_name: "Stage Lights Command",
              warnings: [{ field: "group_address", code: "duplicate_existing", message: 'duplicate of 1/0/1 — exists as "Stage Lights Command"' }],
            },
            {
              row_number: 2,
              group_address: "1/0/2",
              name: "Foyer",
              description: null,
              dpt: "1.001",
              importable: true,
              existing_id: null,
              existing_name: null,
              warnings: [],
            },
          ],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(200, { added: ["1/0/2"], updated: [], skipped: ["1/0/1"], added_count: 1, updated_count: 0, skipped_count: 1 }),
      );

    renderWithProviders(<ImportWizard open onOpenChange={() => {}} />);
    await chooseFile(file("export.csv", "Main,Middle,Sub,Name,Description,Data Type\n1,0,1,Stage,,1.001\n1,0,2,Foyer,,1.001\n"));
    fireEvent.click(await screen.findByRole("button", { name: "Continue" }));
    fireEvent.click(await screen.findByRole("button", { name: /^Import \d+ address/ }));

    expect(await screen.findByText("1 added, 0 updated, 1 skipped.")).toBeInTheDocument();
    const confirmCall = fetchMock.mock.calls[1]!;
    const confirmForm = confirmCall[1].body as FormData;
    expect(confirmForm.get("step")).toBe("confirm");
    expect(confirmForm.get("duplicate_strategy")).toBe("skip");
  });
});
