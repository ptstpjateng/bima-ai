import type { Metadata } from "next";
import { Plus_Jakarta_Sans, Lexend } from "next/font/google";
import { cn } from "@/lib/utils";
import { ThemeScript } from "@/components/ThemeScript";
import { MotionProvider } from "@/components/MotionProvider";
import "./globals.css";

// BIMA design system fonts (see BIMA-Vault/BIMA Design System.md).
// Plus Jakarta Sans = display — geometric, echoes the lowercase "bima"
//   wordmark; Indonesian-made, fitting for a Jawa Tengah public service.
// Lexend = body/UI — engineered for reading proficiency (accessibility),
//   the skill's recommendation for government surfaces.
const display = Plus_Jakarta_Sans({
  subsets: ["latin"],
  variable: "--font-display",
  weight: ["500", "600", "700", "800"],
  display: "swap",
});

const sans = Lexend({
  subsets: ["latin"],
  variable: "--font-sans",
  weight: ["300", "400", "500", "600"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "BIMA — Asisten AI Perizinan UMKM",
  description:
    "BIMA adalah asisten AI yang membantu pelaku UMKM Jawa Tengah memahami KBLI, persyaratan PB UMKU, dan alur OSS RBA langsung dari WhatsApp. Resmi DPMPTSP Provinsi Jawa Tengah.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="id"
      className={cn(display.variable, sans.variable)}
      suppressHydrationWarning
    >
      <head>
        {/* Applies the stored theme synchronously before first paint so there
            is no flash of the wrong palette. Mirrors ThemeToggle's resolver. */}
        <ThemeScript />
      </head>
      {/* Light-first per the Bima Guidelines; dark mode follows the OS via
          prefers-color-scheme, overridden by the .dark/.light toggle in the
          top bar (persisted to localStorage `bima-theme`). */}
      <body className="bg-bg text-ink antialiased min-h-screen">
        <MotionProvider>{children}</MotionProvider>
      </body>
    </html>
  );
}
