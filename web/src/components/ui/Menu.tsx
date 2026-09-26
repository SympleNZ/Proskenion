/* Popover menu over Radix DropdownMenu (shadcn/ui underneath). Items are 44 px. */
import { DropdownMenu } from "radix-ui";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export const Menu = DropdownMenu.Root;
export const MenuTrigger = DropdownMenu.Trigger;

export function MenuContent({ className, ...props }: ComponentProps<typeof DropdownMenu.Content>) {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content className={cn("menu-content", className)} sideOffset={8} collisionPadding={8} {...props} />
    </DropdownMenu.Portal>
  );
}

export function MenuItem({ className, ...props }: ComponentProps<typeof DropdownMenu.Item>) {
  return <DropdownMenu.Item className={cn("menu-item", className)} {...props} />;
}

export function MenuLabel({ className, ...props }: ComponentProps<typeof DropdownMenu.Label>) {
  return <DropdownMenu.Label className={cn("menu-label", className)} {...props} />;
}

export function MenuSeparator({ className, ...props }: ComponentProps<typeof DropdownMenu.Separator>) {
  return <DropdownMenu.Separator className={cn("menu-separator", className)} {...props} />;
}
