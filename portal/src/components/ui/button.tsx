import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Slot } from "radix-ui";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "group/button inline-flex shrink-0 cursor-pointer items-center justify-center rounded-pill border border-transparent bg-clip-padding text-sm font-medium whitespace-nowrap transition-colors duration-200 outline-none select-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-ink disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        // Primary action — brand olive, white label (AA ~5:1).
        primary:
          "bg-primary text-white hover:bg-primary-hover active:translate-y-px",
        // Persistent "chat / go" CTA — forest green, white label (AA ~9.5:1).
        // Kept under the legacy name `amber` (the top bar uses it) but is now
        // the inviting WhatsApp/action green of the Bima system.
        amber:
          "bg-brand-green text-white hover:bg-brand-green-hover active:translate-y-px",
        // Quiet action — sits on the page, fills on hover.
        ghost:
          "text-ink-2 hover:text-ink hover:bg-surface-2",
        link: "text-accent-ink underline-offset-4 hover:underline",
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
