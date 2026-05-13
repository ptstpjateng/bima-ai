import type { Metadata } from "next";
import { Jost, Work_Sans } from "next/font/google";
import { cn } from "@/lib/utils";
import { Providers } from "./providers";
import "./globals.css";

// Brand-locked fonts per BIMA-Vault/Brand.md.
// Jost = display (Futura alternative, geometric).
// Work Sans = body / UI (humanist sans).
const jost = Jost({
  subsets: ["latin"],
  variable: "--font-display",
  weight: ["400", "500", "600", "700"],
  display: "swap",
});

const workSans = Work_Sans({
  subsets: ["latin"],
  variable: "--font-sans",
  weight: ["400", "500", "600"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "BIMA Admin Console",
  description: "DPMPTSP Jawa Tengah — BIMA Admin Console",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={cn("dark", jost.variable, workSans.variable)}
      suppressHydrationWarning
    >
      <body className="font-sans bg-surface-base text-text-primary antialiased min-h-screen">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
