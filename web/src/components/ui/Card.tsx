/* Card (spec §21.3 quick reference): bg surface, radius md, hairline border. */
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface CardProps extends Omit<ComponentProps<"section">, "title"> {
  title?: ReactNode;
  compact?: boolean;
  /**
   * The heading level for `title`, independent of the `.card-title` class
   * that gives it its look (spec §24, axe `heading-order`). Most screens
   * wrap their cards in an `h2` "section title" first, so the default `h3`
   * continues that outline correctly. A screen where the card is the first
   * heading after the screen's own `h1` — nothing else between them —
   * passes `titleLevel="h2"` instead, so the document never skips a level.
   */
  titleLevel?: "h2" | "h3";
}

export function Card({ title, compact, titleLevel = "h3", className, children, ...props }: CardProps) {
  const Title = titleLevel;
  return (
    <section className={cn("card flex flex-col gap-3", compact && "card-compact", className)} {...props}>
      {title ? <Title className="card-title">{title}</Title> : null}
      {children}
    </section>
  );
}
