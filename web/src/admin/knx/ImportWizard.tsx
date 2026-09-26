/*
 * The bulk import wizard (§7.1 *Bulk import*, §21.19 *Import wizard*): three
 * steps — upload, map and preview, confirm — over the two-step `/knx/import`
 * endpoint (preview, then confirm with a token). §7.1: "The import runs in a
 * single transaction, all or nothing" — a failed confirm writes nothing, and
 * this wizard never claims otherwise.
 *
 * The mapping step only appears for a generic CSV/TSV: ETS CSV and XML carry
 * a fixed, known column layout the server fills in automatically (§7.1), so
 * this wizard reads the file's header row itself (`sniff.ts`) to decide,
 * before it ever asks the server for a preview — the server refuses a
 * generic file with no mapping outright, so there is no server round trip
 * that could supply the columns to map from.
 */
import { useRef, useState, type DragEvent } from "react";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";

import { useConfirmImport, usePreviewImport } from "./api";
import { looksLikeEtsCsv, peekFile } from "./sniff";
import {
  DIRECTIONS,
  DIRECTION_LABELS,
  DUPLICATE_STRATEGIES,
  MAPPABLE_TARGETS,
  type ConfirmResponse,
  type Direction,
  type DuplicateStrategy,
  type ImportFormat,
  type MappableTarget,
  type PreviewResponse,
} from "./types";

type WizardState =
  | { step: "upload" }
  | { step: "mapping"; file: File; header: string[]; sample: string[] }
  | { step: "preview"; response: PreviewResponse }
  | { step: "confirm"; response: PreviewResponse }
  | { step: "summary"; result: ConfirmResponse };

const TARGET_LABELS: Readonly<Record<MappableTarget, string>> = {
  group_address: "Group address",
  name: "Name",
  description: "Description",
  dpt: "DPT",
  skip: "Skip",
};

function refusalMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

// -- step 1: upload -------------------------------------------------------------

function UploadStep({ onChoose, pending }: { onChoose: (file: File) => void; pending: boolean }) {
  const fileInput = useRef<HTMLInputElement>(null);

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    const file = event.dataTransfer.files[0];
    if (file) onChoose(file);
  }

  return (
    <div className="device-section" onDragOver={(event) => event.preventDefault()} onDrop={onDrop} data-testid="knx-import-dropzone">
      <p className="view-lede">
        Drag and drop a file here, or choose one. ETS CSV/TSV, ETS 5/6 group address XML, or a generic CSV/TSV. 5 MB
        limit. The older ETS 3/4 .esf format is not supported.
      </p>
      <input
        ref={fileInput}
        type="file"
        accept=".csv,.tsv,.xml,.esf,text/csv,text/tab-separated-values,application/xml,text/xml"
        aria-label="Choose a file to import"
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file) onChoose(file);
          event.currentTarget.value = "";
        }}
        style={{ display: "none" }}
      />
      <Button variant="primary" helpId="knx.import.choose-file" onClick={() => fileInput.current?.click()} loading={pending}>
        Choose a file
      </Button>
    </div>
  );
}

// -- step 2a: map columns (generic CSV/TSV only) ---------------------------------

function MappingStep({
  file,
  header,
  sample,
  onContinue,
  onCancel,
  pending,
}: {
  file: File;
  header: string[];
  sample: string[];
  onContinue: (mapping: Record<string, string>) => void;
  onCancel: () => void;
  pending: boolean;
}) {
  const [mapping, setMapping] = useState<Record<string, MappableTarget>>(() => Object.fromEntries(header.map((column) => [column, "skip"])));

  const countMapped = (target: MappableTarget) => Object.values(mapping).filter((value) => value === target).length;
  const canContinue = countMapped("group_address") === 1 && countMapped("dpt") === 1;

  return (
    <div className="device-section">
      <p className="view-lede">
        {file.name} does not look like an ETS export. Map its columns to the address library's fields — group
        address and DPT are required.
      </p>
      <div className="table-scroll">
        <table className="data-table">
          <caption className="sr-only">Column mapping for {file.name}</caption>
          <thead>
            <tr>
              <th scope="col">Column</th>
              <th scope="col">Maps to</th>
              <th scope="col">Sample</th>
            </tr>
          </thead>
          <tbody>
            {header.map((column, index) => (
              <tr key={column}>
                <td>{column}</td>
                <td>
                  <Select
                    aria-label={`${column} maps to`}
                    value={mapping[column]}
                    onChange={(event) => {
                      // The target is read synchronously — a React SyntheticEvent's
                      // `currentTarget` is cleared once the handler returns, so it
                      // cannot be read lazily from inside the state updater below.
                      const target = event.currentTarget.value as MappableTarget;
                      setMapping((current) => ({ ...current, [column]: target }));
                    }}
                  >
                    {MAPPABLE_TARGETS.map((target) => (
                      <option key={target} value={target}>
                        {TARGET_LABELS[target]}
                      </option>
                    ))}
                  </Select>
                </td>
                <td className="technical">{sample[index] ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!canContinue ? <p className="field-note">Map exactly one column each to group address and DPT to continue.</p> : null}
      <div className="dialog-actions">
        <Button variant="secondary" onClick={onCancel}>
          Choose a different file
        </Button>
        <Button
          variant="primary"
          helpId="knx.import.mapping-continue"
          disabled={!canContinue}
          loading={pending}
          onClick={() => {
            const sourceToTarget: Record<string, string> = {};
            for (const [column, target] of Object.entries(mapping)) {
              if (target !== "skip") sourceToTarget[column] = target;
            }
            onContinue(sourceToTarget);
          }}
        >
          Continue
        </Button>
      </div>
    </div>
  );
}

// -- step 2b: preview with inline warnings ---------------------------------------

function PreviewStep({
  response,
  direction,
  onDirectionChange,
  onChooseAnother,
  onContinue,
}: {
  response: PreviewResponse;
  direction: Direction;
  onDirectionChange: (direction: Direction) => void;
  onChooseAnother: () => void;
  onContinue: () => void;
}) {
  return (
    <div className="device-section">
      <p className="view-lede">
        {response.filename} · {response.row_count} row{response.row_count === 1 ? "" : "s"} ·{" "}
        {response.importable_count} importable
        {response.duplicate_count > 0 ? ` · ${response.duplicate_count} duplicate of an existing address` : ""}
      </p>
      <div className="table-scroll">
        <table className="data-table">
          <caption className="sr-only">Preview of every row, warnings included</caption>
          <thead>
            <tr>
              <th scope="col">Row</th>
              <th scope="col">Address</th>
              <th scope="col">Name</th>
              <th scope="col">DPT</th>
              <th scope="col">Warnings</th>
            </tr>
          </thead>
          <tbody>
            {response.rows.map((row) => (
              <tr key={row.row_number} data-importable={row.importable}>
                <td>{row.row_number}</td>
                <td className="technical">{row.group_address}</td>
                <td>{row.name}</td>
                <td className="technical">{row.dpt}</td>
                <td>
                  {row.warnings.length === 0 ? null : (
                    <ul>
                      {row.warnings.map((warning) => (
                        <li key={warning.code} className="flag" data-tone="warning">
                          {warning.message}
                        </li>
                      ))}
                    </ul>
                  )}
                  {!row.importable ? <span className="field-note">Not imported</span> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="field schema-field">
        <FieldLabel htmlFor="knx-import-direction" help="knx.import.direction">
          Direction for all rows
        </FieldLabel>
        <p className="field-note">ETS's export has no concept of direction, so one choice applies to the whole import.</p>
        <Select id="knx-import-direction" value={direction} onChange={(event) => onDirectionChange(event.currentTarget.value as Direction)}>
          {DIRECTIONS.map((value) => (
            <option key={value} value={value}>
              {DIRECTION_LABELS[value]}
            </option>
          ))}
        </Select>
      </div>

      <div className="dialog-actions">
        <Button variant="secondary" onClick={onChooseAnother}>
          Choose a different file
        </Button>
        <Button variant="primary" helpId="knx.import.preview-continue" disabled={response.importable_count === 0} onClick={onContinue}>
          Continue
        </Button>
      </div>
    </div>
  );
}

// -- step 3: confirm --------------------------------------------------------------

function ConfirmStep({
  response,
  duplicateStrategy,
  onStrategyChange,
  onCancel,
  onConfirm,
  pending,
}: {
  response: PreviewResponse;
  duplicateStrategy: DuplicateStrategy;
  onStrategyChange: (strategy: DuplicateStrategy) => void;
  onCancel: () => void;
  onConfirm: () => void;
  pending: boolean;
}) {
  const duplicates = response.rows.filter((row) => row.existing_id !== null);
  const willWrite = duplicateStrategy === "skip" ? response.importable_count - response.duplicate_count : response.importable_count;

  return (
    <div className="device-section">
      <p className="view-lede">
        Import will add {response.importable_count - response.duplicate_count} address
        {response.importable_count - response.duplicate_count === 1 ? "" : "es"}.
      </p>
      {duplicates.length > 0 ? (
        <>
          <p>
            {duplicates.length} row{duplicates.length === 1 ? " is a" : "s are"} duplicate
            {duplicates.length === 1 ? "" : "s"} of existing entries:
          </p>
          <ul className="review-list">
            {duplicates.map((row) => (
              <li key={row.row_number} className="review-row technical">
                {row.group_address} — exists as &quot;{row.existing_name}&quot;
              </li>
            ))}
          </ul>
          <fieldset className="field schema-field">
            <legend className="field-label">Duplicates</legend>
            <HelpButton id="knx.import.duplicates" />
            {DUPLICATE_STRATEGIES.map((strategy) => (
              <label className="radio" key={strategy} htmlFor={`knx-import-strategy-${strategy}`}>
                <span className="radio-mark" aria-hidden="true" />
                <input
                  type="radio"
                  id={`knx-import-strategy-${strategy}`}
                  name="knx-import-strategy"
                  checked={duplicateStrategy === strategy}
                  onChange={() => onStrategyChange(strategy)}
                />
                <span>{strategy === "skip" ? "Skip them" : "Overwrite with imported"}</span>
              </label>
            ))}
          </fieldset>
        </>
      ) : null}
      <div className="dialog-actions">
        <Button variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button variant="primary" helpId="knx.import.confirm" onClick={onConfirm} loading={pending}>
          Import {willWrite} address{willWrite === 1 ? "" : "es"}
        </Button>
      </div>
    </div>
  );
}

// -- step 4: summary ---------------------------------------------------------------

function SummaryStep({ result, onDone }: { result: ConfirmResponse; onDone: () => void }) {
  return (
    <div className="device-section">
      <Banner tone="success" title="Import complete">
        {result.added_count} added, {result.updated_count} updated, {result.skipped_count} skipped.
      </Banner>
      <dl className="kv">
        <dt>Added</dt>
        <dd className="technical">{result.added.join(", ") || "—"}</dd>
        <dt>Updated</dt>
        <dd className="technical">{result.updated.join(", ") || "—"}</dd>
        <dt>Skipped</dt>
        <dd className="technical">{result.skipped.join(", ") || "—"}</dd>
      </dl>
      <div className="dialog-actions">
        <Button variant="primary" helpId="knx.import.done" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

// -- the wizard itself --------------------------------------------------------------

export interface ImportWizardProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ImportWizard({ open, onOpenChange }: ImportWizardProps) {
  const [state, setState] = useState<WizardState>({ step: "upload" });
  const [refusal, setRefusal] = useState<string | undefined>();
  const [direction, setDirection] = useState<Direction>("incoming");
  const [duplicateStrategy, setDuplicateStrategy] = useState<DuplicateStrategy>("skip");
  const preview = usePreviewImport();
  const confirm = useConfirmImport();

  // Reset to step one whenever the sheet is closed, so reopening it always
  // starts a fresh import rather than resuming a half-finished one. React's
  // documented "adjusting state when a prop changes" pattern — comparing
  // against the previous render's `open` and calling setState conditionally
  // during render — not an effect, since there is nothing external to
  // synchronise with here.
  const [wasOpen, setWasOpen] = useState(open);
  if (wasOpen !== open) {
    setWasOpen(open);
    if (!open) {
      setState({ step: "upload" });
      setRefusal(undefined);
      setDirection("incoming");
      setDuplicateStrategy("skip");
    }
  }

  function runPreview(file: File, format?: ImportFormat, mapping?: Record<string, string>) {
    setRefusal(undefined);
    preview.mutate(
      { file, format, mapping },
      {
        onSuccess: (response) => setState({ step: "preview", response }),
        onError: (error) => {
          setRefusal(refusalMessage(error, "The file could not be read — the controller did not answer."));
          setState({ step: "upload" });
        },
      },
    );
  }

  async function chooseFile(file: File) {
    setRefusal(undefined);
    if (file.name.toLowerCase().endsWith(".esf")) {
      // Only the server's own message is shown for this refusal (§21.19), so
      // the file is still sent rather than refused here from a guessed reason.
      runPreview(file);
      return;
    }
    const peek = await peekFile(file);
    if (peek.looksXml) {
      runPreview(file, "ets_xml");
      return;
    }
    if (looksLikeEtsCsv(peek.header)) {
      runPreview(file, "ets_csv");
      return;
    }
    setState({ step: "mapping", file, header: peek.header, sample: peek.sample });
  }

  function runConfirm(token: string) {
    setRefusal(undefined);
    confirm.mutate(
      { token, direction, duplicateStrategy },
      {
        onSuccess: (result) => setState({ step: "summary", result }),
        onError: (error) => {
          if (error instanceof ApiError && error.code === "not_found") {
            setRefusal(`${error.message} Nothing was imported.`);
            setState({ step: "upload" });
            return;
          }
          setRefusal(refusalMessage(error, "The import could not be confirmed — nothing was imported."));
        },
      },
    );
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title="Import the group address library"
        description="Upload an ETS export or a generic CSV, review every row, then confirm. All or nothing — a failed import changes nothing."
      >
        <div className="wizard-panel">
          {refusal ? (
            <Banner tone="danger" title="This file could not be imported">
              {refusal}
            </Banner>
          ) : null}

          {state.step === "upload" ? <UploadStep onChoose={(file) => void chooseFile(file)} pending={preview.isPending} /> : null}

          {state.step === "mapping" ? (
            <MappingStep
              file={state.file}
              header={state.header}
              sample={state.sample}
              onContinue={(mapping) => runPreview(state.file, "generic", mapping)}
              onCancel={() => setState({ step: "upload" })}
              pending={preview.isPending}
            />
          ) : null}

          {state.step === "preview" ? (
            <PreviewStep
              response={state.response}
              direction={direction}
              onDirectionChange={setDirection}
              onChooseAnother={() => setState({ step: "upload" })}
              onContinue={() => setState({ step: "confirm", response: state.response })}
            />
          ) : null}

          {state.step === "confirm" ? (
            <ConfirmStep
              response={state.response}
              duplicateStrategy={duplicateStrategy}
              onStrategyChange={setDuplicateStrategy}
              onCancel={() => setState({ step: "preview", response: state.response })}
              onConfirm={() => runConfirm(state.response.token)}
              pending={confirm.isPending}
            />
          ) : null}

          {state.step === "summary" ? <SummaryStep result={state.result} onDone={() => onOpenChange(false)} /> : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}
