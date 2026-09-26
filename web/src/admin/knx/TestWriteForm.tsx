/*
 * Test write (§21.19 *Test write*): "an inline mini-form with a value field
 * typed to the DPT and an immediate result." Available from an address's row
 * or its editor — both callers just mount this with the address.
 *
 * This acts on the building (a real KNX write reaches real hardware), so it
 * always confirms first, naming the address, and an incoming-only address
 * never reaches the confirmation at all: the control is disabled with the
 * reason, because the server would refuse it anyway (`IncomingOnlyAddress`).
 */
import { useId, useState } from "react";

import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Select";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { FieldLabel } from "@/help/HelpButton";

import { useTestWrite } from "./api";
import { dptFamily } from "./dpt";
import type { KnxAddress } from "./types";

type Control = "bool" | "dimming" | "percent" | "byte" | "float";

function controlFor(dpt: string): Control {
  const main = Number(dptFamily(dpt));
  if (main === 1) return "bool";
  if (main === 3) return "dimming";
  if (main === 5) return dpt === "5.001" ? "percent" : "byte";
  if (main === 9) return "float";
  return "byte"; // 20.x, and anything else the address form could not have offered
}

type ControlValue = boolean | number | { increase: boolean; step_code: number };

function defaultValue(control: Control): ControlValue {
  switch (control) {
    case "bool":
      return false;
    case "dimming":
      return { increase: true, step_code: 1 };
    case "percent":
      return 0;
    case "float":
      return 0;
    case "byte":
      return 0;
  }
}

export interface TestWriteFormProps {
  address: KnxAddress;
}

export function TestWriteForm({ address }: TestWriteFormProps) {
  const control = controlFor(address.dpt);
  const [value, setValue] = useState<ControlValue>(() => defaultValue(control));
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<string | undefined>();
  const [failure, setFailure] = useState<string | undefined>();
  const testWrite = useTestWrite();
  const idPrefix = useId();

  const disabled = address.direction === "incoming";

  function send() {
    setResult(undefined);
    setFailure(undefined);
    testWrite.mutate(
      { addressId: address.id, value },
      {
        onSuccess: () => setResult(`Sent to ${address.group_address}.`),
        onError: (error) => {
          setFailure(error instanceof ApiError ? error.message : "The test write could not be sent — the controller did not answer.");
        },
      },
    );
  }

  if (disabled) {
    return (
      <div className="test-write" data-testid={`test-write-${address.id}`}>
        <Button variant="secondary" disabled>
          Test write
        </Button>
        <p className="field-note">Incoming-only — the system only listens on this address; it cannot be written.</p>
      </div>
    );
  }

  return (
    <div className="test-write" data-testid={`test-write-${address.id}`}>
      <div className="field schema-field">
        <FieldLabel htmlFor={`${idPrefix}-value`} help="knx.test-write.value">
          Value ({address.dpt})
        </FieldLabel>
        {control === "bool" ? (
          <Checkbox
            id={`${idPrefix}-value`}
            label="On"
            checked={value === true}
            onChange={(event) => setValue(event.currentTarget.checked)}
          />
        ) : control === "dimming" ? (
          <div className="flex gap-3">
            <Checkbox
              id={`${idPrefix}-increase`}
              label="Increase"
              checked={(value as { increase: boolean }).increase}
              onChange={(event) => {
                const increase = event.currentTarget.checked;
                setValue((current) => ({ ...(current as { step_code: number }), increase }));
              }}
            />
            <input
              className="input"
              id={`${idPrefix}-step`}
              type="number"
              min={0}
              max={7}
              value={(value as { step_code: number }).step_code}
              onChange={(event) => {
                const stepCode = Number(event.currentTarget.value);
                setValue((current) => ({ ...(current as { increase: boolean }), step_code: stepCode }));
              }}
              aria-label="Step code, 0 to 7"
            />
          </div>
        ) : (
          <input
            className="input"
            id={`${idPrefix}-value`}
            type="number"
            step={control === "percent" ? 0.1 : control === "float" ? "any" : 1}
            min={control === "percent" ? 0 : undefined}
            max={control === "percent" ? 100 : control === "byte" ? 255 : undefined}
            value={value as number}
            onChange={(event) => setValue(Number(event.currentTarget.value))}
          />
        )}
      </div>

      <Button variant="secondary" onClick={() => setConfirming(true)} loading={testWrite.isPending}>
        Test write
      </Button>

      {result ? (
        <Banner tone="success" title="Sent">
          {result}
        </Banner>
      ) : null}
      {failure ? (
        <Banner tone="danger" title="The write failed">
          {failure}
        </Banner>
      ) : null}

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Write to "${address.name}"?`}
        description={`This sends a real telegram to ${address.group_address} on the building's KNX bus. It is not a simulation.`}
        confirmLabel="Send"
        onConfirm={() => {
          setConfirming(false);
          send();
        }}
      />
    </div>
  );
}
