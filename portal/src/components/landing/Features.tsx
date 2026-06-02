"use client";

import { Clock, FileCheck, Search } from "lucide-react";
import { motion } from "framer-motion";

const FEATURES = [
  {
    icon: Search,
    title: "Cek KBLI",
    description:
      "Tahu kode KBLI usaha Anda dalam hitungan detik. Cukup ceritakan jenis usaha.",
  },
  {
    icon: FileCheck,
    title: "Persyaratan Lengkap",
    description:
      "Daftar dokumen, jangka waktu, dan kewajiban PB UMKU untuk setiap KBLI Anda.",
  },
  {
    icon: Clock,
    title: "Konsultasi 24/7",
    description:
      "Tanya kapan saja. BIMA dilatih dari regulasi OSS RBA resmi pemerintah.",
  },
] as const;

/**
 * Feature grid — 3 cards, equal width on md+, stacked on mobile.
 * Cards lift gently on hover per Brand.md motion spec (scale 1→1.01).
 */
export function Features() {
  return (
    <section id="fitur" className="px-4 py-16 sm:px-6 sm:py-20 md:py-28 lg:px-8">
      <div className="mx-auto max-w-6xl">
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.2, ease: "easeOut" }}
          className="mb-12 max-w-2xl"
        >
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-brand-green-text">
            Fitur
          </p>
          <h2 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink sm:text-3xl md:text-4xl">
            Semua yang Anda butuhkan untuk memulai usaha.
          </h2>
        </motion.div>

        <div className="grid gap-6 md:grid-cols-3">
          {FEATURES.map(({ icon: Icon, title, description }, i) => (
            <motion.article
              key={title}
              initial={{ opacity: 0, y: 4 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-80px" }}
              transition={{
                duration: 0.2,
                ease: "easeOut",
                delay: i * 0.05,
              }}
              whileHover={{ scale: 1.01 }}
              className="group rounded-card border border-border bg-surface p-6 transition-all duration-200 hover:border-border-strong hover:bg-surface-hover hover:shadow-[0_8px_24px_-10px_rgba(31,77,51,0.28)]"
            >
              <div className="mb-5 inline-flex h-10 w-10 items-center justify-center rounded-pill bg-brand-green/10">
                <Icon className="size-5 text-brand-green" aria-hidden="true" />
              </div>
              <h3 className="font-display text-lg font-semibold text-ink">
                {title}
              </h3>
              <p className="mt-2 text-sm leading-relaxed text-ink-2">
                {description}
              </p>
            </motion.article>
          ))}
        </div>
      </div>
    </section>
  );
}
