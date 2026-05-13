import type { Metadata } from "next";
import { Jost, Work_Sans } from "next/font/google";
import { cn } from "@/lib/utils";
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
      className={cn("dark", jost.variable, workSans.variable)}
      suppressHydrationWarning
    >
      <body className="bg-surface-base text-text-primary antialiased min-h-screen">
        {children}
      </body>
    </html>
  );
}
