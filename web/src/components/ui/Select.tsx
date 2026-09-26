/*
 * Select and checkbox (spec §21.3 quick reference, §24.2). Native controls on
 * purpose: they are keyboard reachable, they carry their own labelling, and
 * an enum from a driver schema is a list of values, not a design problem.
 */
import { Check } from "lucide-react";
import type { ComponentProps, Ref } from "react";

import { cn } from "@/lib/utils";

export interface SelectProps extends ComponentProps<"select"> {
  ref?: Ref<HTMLSelectElement>;
}

export function Select({ className, ...props }: SelectProps) {
  return <select className={cn("select", className)} {...props} />;
}

export interface CheckboxProps extends Omit<ComponentProps<"input">, "type"> {
  label: string;
  ref?: Ref<HTMLInputElement>;
}

/** A checkbox whose state is a tick as well as a colour (§24.1). */
export function Checkbox({ className, label, id, ...props }: CheckboxProps) {
  return (
    <label className={cn("checkbox", className)} htmlFor={id}>
      <span className="checkbox-box" aria-hidden="true">
        <Check className="size-4" strokeWidth={3} />
      </span>
      <input type="checkbox" id={id} {...props} />
      <span>{label}</span>
    </label>
  );
}
