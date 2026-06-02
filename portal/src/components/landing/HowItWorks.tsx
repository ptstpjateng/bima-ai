"use client";

import { motion } from "framer-motion";

const STEPS = [
  "Kirim 'Halo' ke WhatsApp BIMA",
  "Ceritakan jenis usaha Anda",
  "Dapatkan jawaban lengkap dan langkah berikutnya",
] as const;

/**
 * Three numbered steps. Side-by-side on md+, stacked on mobile.
 * Step number lives inside a small navy circular badge in display font.
 */
export function HowItWorks() {
  return (
    <section className="px-4 py-16 sm:px-6 sm:py-20 md:py-28 lg:px-8">
      <div className="mx-auto max-w-6xl">
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.2, ease: "easeOut" }}
          className="mb-12 max-w-2xl"
        >
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-brand-green-text">
            Cara kerja
          </p>
          <h2 className="mt-3 font-display text-2xl font-semibold tracking-tight text-ink sm:text-3xl md:text-4xl">
            Tiga langkah, selesai.
          </h2>
        </motion.div>

        <ol className="grid gap-6 md:grid-cols-3">
          {STEPS.map((step, i) => (
            <motion.li
              key={step}
              initial={{ opacity: 0, y: 4 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-80px" }}
              transition={{
                duration: 0.2,
                ease: "easeOut",
                delay: i * 0.05,
              }}
              className="flex flex-col items-start"
            >
              <span
                aria-hidden="true"
                className="inline-flex h-10 w-10 items-center justify-center rounded-pill border border-primary/30 bg-primary/15 font-display text-base font-semibold text-ink"
              >
                {i + 1}
              </span>
              <p className="mt-5 text-base leading-relaxed text-ink">
                {step}
              </p>
            </motion.li>
          ))}
        </ol>
      </div>
    </section>
  );
}
