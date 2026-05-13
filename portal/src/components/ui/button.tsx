import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center rounded-pill border border-transparent bg-clip-padding text-sm font-medium whitespace-nowrap transition-all outline-none select-none focus-visible:ring-2 focus-visible:ring-brand-navy/60 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        // Primary navy CTA — the standard BIMA action.
        primary:
          "bg-brand-navy text-text-primary hover:bg-brand-navy-hover hover:shadow-[0_8px_24px_-8px_rgba(46,77,204,0.45)] active:translate-y-px",
        // Amber wordmark/nav CTA — used in the top bar.
        amber:
          "bg-brand-amber text-surface-base hover:bg-brand-amber-hover active:translate-y-px",
        ghost:
          "text-text-secondary hover:text-text-primary hover:bg-surface-card",
        link: "text-text-secondary underline-offset-4 hover:underline hover:text-text-primary",
      },
      size: {
        sm: "h-9 gap-1.5 px-4 text-sm",
        md: "h-11 gap-2 px-5 text-sm",
        lg: "h-12 gap-2 px-6 text-base",
      },
    },
    defaultVariants: {
      variant: "primary",
      size: "md",
    },
  }
);

function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot.Root : "button";

  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Button, buttonVariants };
