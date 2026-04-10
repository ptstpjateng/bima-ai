"use client";

import {
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Loader2,
  Search,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { BusinessScale, KbliOption, PermitApplyPayload, RiskLevel } from "@/types";
import { applyPermit, searchKbli } from "@/lib/api";
import {
  RISK_LABELS,
  SCALE_LABELS,
  cn,
  formatCurrency,
} from "@/lib/utils";

// ── Types ──────────────────────────────────────────────────────────────────────

interface WizardState {
  kbli_code: string;
  kbli_description: string;
  kbli_section: string;
  business_scale: BusinessScale | "";
  risk_level: RiskLevel | "";
  annual_revenue_estimate: string;
  employee_count: string;
  business_location_province: string;
  business_location_city: string;
  business_location_address: string;
  applicant_notes: string;
}

const INITIAL_STATE: WizardState = {
  kbli_code: "",
  kbli_description: "",
  kbli_section: "",
  business_scale: "",
  risk_level: "",
  annual_revenue_estimate: "",
  employee_count: "",
  business_location_province: "",
  business_location_city: "",
  business_location_address: "",
  applicant_notes: "",
};

// ── Step indicators ────────────────────────────────────────────────────────────

const STEPS = [
  "Klasifikasi Usaha",
  "Skala & Risiko",
  "Lokasi Usaha",
  "Konfirmasi",
] as const;

interface StepIndicatorProps {
  current: number;
}

function StepIndicator({ current }: StepIndicatorProps) {
  return (
    <div className="mb-8">
      <div className="flex items-center justify-between">
        {STEPS.map((label, idx) => {
          const done = idx < current;
          const active = idx === current;
          return (
            <div key={label} className="flex flex-1 items-center">
              <div className="flex flex-col items-center">
                <div
                  className={cn(
                    "flex h-8 w-8 items-center justify-center rounded-full text-sm font-bold transition-colors",
                    done
                      ? "bg-green-500 text-white"
                      : active
                        ? "bg-brand-700 text-white"
                        : "bg-gray-100 text-gray-400",
                  )}
                >
                  {done ? <CheckCircle2 className="h-4 w-4" /> : idx + 1}
                </div>
                <p
                  className={cn(
                    "mt-1 hidden text-center text-[10px] font-medium sm:block",
                    active ? "text-brand-700" : done ? "text-green-600" : "text-gray-400",
                  )}
                >
                  {label}
                </p>
              </div>
              {idx < STEPS.length - 1 && (
                <div
                  className={cn(
                    "mx-1 h-0.5 flex-1 transition-colors",
                    idx < current ? "bg-green-400" : "bg-gray-200",
                  )}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Input helpers ──────────────────────────────────────────────────────────────

function Label({ htmlFor, children, required }: { htmlFor: string; children: React.ReactNode; required?: boolean }) {
  return (
    <label htmlFor={htmlFor} className="mb-1.5 block text-sm font-medium text-gray-700">
      {children}
      {required && <span className="ml-1 text-red-500">*</span>}
    </label>
  );
}

function Input({
  id, value, onChange, placeholder, type = "text", required,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  required?: boolean;
}) {
  return (
    <input
      id={id}
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      required={required}
      className="w-full rounded-xl border border-gray-200 bg-white px-4 py-2.5 text-sm text-gray-900 placeholder-gray-400 outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100"
    />
  );
}

function Textarea({ id, value, onChange, placeholder }: {
  id: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <textarea
      id={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      rows={3}
      className="w-full rounded-xl border border-gray-200 bg-white px-4 py-2.5 text-sm text-gray-900 placeholder-gray-400 outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100"
    />
  );
}

// ── Step 1: KBLI Classification with typeahead ─────────────────────────────────

function Step1({ state, update }: { state: WizardState; update: (k: keyof WizardState, v: string) => void }) {
  const [query, setQuery] = useState(state.kbli_code);
  const [results, setResults] = useState<KbliOption[]>([]);
  const [showDropdown, setShowDropdown] = useState(false);
  const [searching, setSearching] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Debounced search
  useEffect(() => {
    if (query.length < 2) {
      setResults([]);
      setShowDropdown(false);
      return;
    }
    setSearching(true);
    const timer = setTimeout(async () => {
      try {
        const data = await searchKbli(query);
        setResults(data);
        setShowDropdown(data.length > 0);
      } catch {
        // silently ignore search errors
      } finally {
        setSearching(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  // Close dropdown on outside click
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  function selectOption(opt: KbliOption) {
    update("kbli_code", opt.kbli_code);
    update("kbli_description", opt.kbli_description ?? "");
    update("kbli_section", opt.sector ?? "");
    setQuery(opt.kbli_code);
    setShowDropdown(false);
  }

  return (
    <div className="space-y-5">
      <div>
        <h3 className="mb-1 text-lg font-bold text-gray-900">
          Klasifikasi Bidang Usaha (KBLI)
        </h3>
        <p className="text-sm text-gray-500">
          Cari kode KBLI berdasarkan kode atau nama bidang usaha Anda.
        </p>
      </div>

      {/* KBLI typeahead */}
      <div ref={containerRef} className="relative">
        <Label htmlFor="kbli_search" required>Kode / Nama KBLI</Label>
        <div className="relative">
          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
          {searching && (
            <Loader2 className="absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-brand-500" />
          )}
          <input
            id="kbli_search"
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              // If user clears the field, reset state fields too
              if (!e.target.value) {
                update("kbli_code", "");
                update("kbli_description", "");
                update("kbli_section", "");
              }
            }}
            onFocus={() => results.length > 0 && setShowDropdown(true)}
            placeholder="Cari: 56102 atau warung makan..."
            autoComplete="off"
            className="w-full rounded-xl border border-gray-200 bg-white py-2.5 pl-10 pr-10 text-sm text-gray-900 placeholder-gray-400 outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100"
          />
        </div>

        {/* Dropdown */}
        {showDropdown && (
          <div className="absolute z-20 mt-1 w-full overflow-hidden rounded-xl border border-gray-200 bg-white shadow-lg">
            {results.map((opt) => (
              <button
                key={opt.kbli_code}
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => selectOption(opt)}
                className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-brand-50 transition-colors"
              >
                <span className="mt-0.5 flex-shrink-0 rounded-md bg-brand-100 px-2 py-0.5 font-mono text-xs font-bold text-brand-700">
                  {opt.kbli_code}
                </span>
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-gray-900">
                    {opt.kbli_description ?? "–"}
                  </p>
                  {opt.sector && (
                    <p className="truncate text-xs text-gray-400">{opt.sector}</p>
                  )}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Selected KBLI preview */}
      {state.kbli_code && (
        <div className="rounded-xl border border-green-200 bg-green-50 p-3">
          <p className="text-xs font-medium text-green-700">KBLI dipilih</p>
          <p className="mt-0.5 text-sm font-semibold text-green-900">
            {state.kbli_code} — {state.kbli_description || "—"}
          </p>
          {state.kbli_section && (
            <p className="text-xs text-green-600">{state.kbli_section}</p>
          )}
        </div>
      )}

      {/* Manual override fields (collapsed under the selected state) */}
      {!state.kbli_code && (
        <div className="space-y-4">
          <div>
            <Label htmlFor="kbli_description" required>Deskripsi Bidang Usaha</Label>
            <Input
              id="kbli_description"
              value={state.kbli_description}
              onChange={(v) => update("kbli_description", v)}
              placeholder="Contoh: Restoran dan Rumah Makan"
            />
          </div>
          <div>
            <Label htmlFor="kbli_section">Sektor / Kelompok KBLI</Label>
            <Input
              id="kbli_section"
              value={state.kbli_section}
              onChange={(v) => update("kbli_section", v)}
              placeholder="Contoh: Penyediaan Makanan dan Minuman"
            />
          </div>
        </div>
      )}

      <div className="rounded-xl border border-brand-100 bg-brand-50 p-4">
        <p className="text-xs font-medium text-brand-700">💡 Tips</p>
        <p className="mt-1 text-xs text-brand-600">
          Gunakan kolom di atas untuk mencari kode KBLI. Jika tidak menemukan kode Anda,
          konsultasikan dengan asisten BIMA-AI atau petugas DPMPTSP.
        </p>
      </div>
    </div>
  );
}

// ── Step 2: Scale & Risk ───────────────────────────────────────────────────────

const SCALES: { value: BusinessScale; label: string; desc: string }[] = [
  { value: "mikro", label: "Usaha Mikro", desc: "Aset ≤ Rp 50 juta, Omset ≤ Rp 300 juta/tahun" },
  { value: "kecil", label: "Usaha Kecil", desc: "Aset Rp 50–500 juta, Omset Rp 300 juta–2,5 miliar/tahun" },
  { value: "menengah", label: "Usaha Menengah", desc: "Aset Rp 500 juta–10 miliar, Omset Rp 2,5–50 miliar/tahun" },
  { value: "besar", label: "Usaha Besar", desc: "Aset > Rp 10 miliar, Omset > Rp 50 miliar/tahun" },
];

const RISKS: { value: RiskLevel; label: string; desc: string; color: string }[] = [
  { value: "rendah", label: "Risiko Rendah", desc: "Hanya perlu NIB — langsung aktif", color: "border-green-200 bg-green-50 text-green-800" },
  { value: "menengah_rendah", label: "Risiko Menengah Rendah", desc: "NIB + Sertifikat Standar (pernyataan mandiri)", color: "border-yellow-200 bg-yellow-50 text-yellow-800" },
  { value: "menengah_tinggi", label: "Risiko Menengah Tinggi", desc: "NIB + Sertifikat Standar (verifikasi pemerintah)", color: "border-orange-200 bg-orange-50 text-orange-800" },
  { value: "tinggi", label: "Risiko Tinggi", desc: "NIB + Izin lengkap dari pemerintah", color: "border-red-200 bg-red-50 text-red-800" },
];

function Step2({ state, update }: { state: WizardState; update: (k: keyof WizardState, v: string) => void }) {
  return (
    <div className="space-y-6">
      <div>
        <h3 className="mb-1 text-lg font-bold text-gray-900">Skala & Tingkat Risiko</h3>
        <p className="text-sm text-gray-500">
          Pilih skala usaha dan tingkat risiko OSS RBA yang sesuai dengan bisnis Anda.
        </p>
      </div>

      <div>
        <Label htmlFor="scale" required>Skala Usaha</Label>
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {SCALES.map((s) => (
            <button
              key={s.value}
              type="button"
              onClick={() => update("business_scale", s.value)}
              className={cn(
                "rounded-xl border p-3 text-left transition-colors",
                state.business_scale === s.value
                  ? "border-brand-400 bg-brand-50 ring-1 ring-brand-300"
                  : "border-gray-200 hover:border-gray-300",
              )}
            >
              <p className="text-sm font-semibold text-gray-900">{s.label}</p>
              <p className="mt-0.5 text-[11px] text-gray-500">{s.desc}</p>
            </button>
          ))}
        </div>
      </div>

      <div>
        <Label htmlFor="risk" required>Tingkat Risiko OSS RBA</Label>
        <div className="mt-2 space-y-2">
          {RISKS.map((r) => (
            <button
              key={r.value}
              type="button"
              onClick={() => update("risk_level", r.value)}
              className={cn(
                "w-full rounded-xl border p-3 text-left transition-all",
                state.risk_level === r.value
                  ? `${r.color} ring-1 ring-inset`
                  : "border-gray-200 hover:border-gray-300",
              )}
            >
              <p className="text-sm font-semibold">{r.label}</p>
              <p className="mt-0.5 text-[11px] text-gray-500">{r.desc}</p>
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <Label htmlFor="revenue">Estimasi Omset Tahunan (IDR)</Label>
          <Input
            id="revenue"
            type="number"
            value={state.annual_revenue_estimate}
            onChange={(v) => update("annual_revenue_estimate", v)}
            placeholder="Contoh: 500000000"
          />
          {state.annual_revenue_estimate && (
            <p className="mt-1 text-xs text-gray-400">
              {formatCurrency(Number(state.annual_revenue_estimate))}
            </p>
          )}
        </div>
        <div>
          <Label htmlFor="employees">Jumlah Karyawan</Label>
          <Input
            id="employees"
            type="number"
            value={state.employee_count}
            onChange={(v) => update("employee_count", v)}
            placeholder="Contoh: 5"
          />
        </div>
      </div>
    </div>
  );
}

// ── Step 3: Location ───────────────────────────────────────────────────────────

function Step3({ state, update }: { state: WizardState; update: (k: keyof WizardState, v: string) => void }) {
  return (
    <div className="space-y-5">
      <div>
        <h3 className="mb-1 text-lg font-bold text-gray-900">Lokasi Usaha</h3>
        <p className="text-sm text-gray-500">
          Masukkan alamat lokasi usaha yang akan didaftarkan.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <Label htmlFor="province">Provinsi</Label>
          <Input
            id="province"
            value={state.business_location_province}
            onChange={(v) => update("business_location_province", v)}
            placeholder="Contoh: Jawa Barat"
          />
        </div>
        <div>
          <Label htmlFor="city">Kota / Kabupaten</Label>
          <Input
            id="city"
            value={state.business_location_city}
            onChange={(v) => update("business_location_city", v)}
            placeholder="Contoh: Bandung"
          />
        </div>
      </div>

      <div>
        <Label htmlFor="address">Alamat Lengkap</Label>
        <Textarea
          id="address"
          value={state.business_location_address}
          onChange={(v) => update("business_location_address", v)}
          placeholder="Jl. Sudirman No. 1, Kelurahan..., Kecamatan..., RT/RW..."
        />
      </div>

      <div>
        <Label htmlFor="notes">Catatan Permohonan (opsional)</Label>
        <Textarea
          id="notes"
          value={state.applicant_notes}
          onChange={(v) => update("applicant_notes", v)}
          placeholder="Informasi tambahan yang ingin Anda sampaikan kepada petugas DPMPTSP..."
        />
      </div>
    </div>
  );
}

// ── Step 4: Confirmation ───────────────────────────────────────────────────────

function Step4({ state }: { state: WizardState }) {
  const rows = [
    ["Kode KBLI", state.kbli_code || "-"],
    ["Bidang Usaha", state.kbli_description || "-"],
    ["Sektor", state.kbli_section || "-"],
    ["Skala Usaha", state.business_scale ? SCALE_LABELS[state.business_scale as BusinessScale] : "-"],
    ["Tingkat Risiko", state.risk_level ? RISK_LABELS[state.risk_level as RiskLevel] : "-"],
    ["Estimasi Omset", state.annual_revenue_estimate ? formatCurrency(Number(state.annual_revenue_estimate)) : "-"],
    ["Jumlah Karyawan", state.employee_count || "-"],
    ["Provinsi", state.business_location_province || "-"],
    ["Kota / Kabupaten", state.business_location_city || "-"],
    ["Alamat Usaha", state.business_location_address || "-"],
    ["Catatan", state.applicant_notes || "-"],
  ];

  return (
    <div className="space-y-5">
      <div>
        <h3 className="mb-1 text-lg font-bold text-gray-900">Konfirmasi Permohonan</h3>
        <p className="text-sm text-gray-500">
          Periksa kembali data Anda sebelum mengirimkan permohonan.
        </p>
      </div>

      <div className="overflow-hidden rounded-xl border border-gray-100">
        {rows.map(([label, value], idx) => (
          <div
            key={label}
            className={cn(
              "flex gap-3 px-4 py-3",
              idx % 2 === 0 ? "bg-gray-50" : "bg-white",
            )}
          >
            <span className="w-36 flex-shrink-0 text-xs font-medium text-gray-500">
              {label}
            </span>
            <span className="text-sm text-gray-800">{value}</span>
          </div>
        ))}
      </div>

      <div className="rounded-xl border border-amber-200 bg-amber-50 p-4">
        <p className="text-xs font-semibold text-amber-800">⚠️ Perhatian</p>
        <p className="mt-1 text-xs text-amber-700">
          Pastikan semua data yang diisi sudah benar. Setelah permohonan dikirim,
          proses peninjauan oleh DPMPTSP akan segera dimulai.
        </p>
      </div>
    </div>
  );
}

// ── Main Wizard ────────────────────────────────────────────────────────────────

export function ApplyWizard() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const [state, setState] = useState<WizardState>(INITIAL_STATE);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState<{ application_number: string } | null>(null);

  function update(key: keyof WizardState, value: string) {
    setState((prev) => ({ ...prev, [key]: value }));
  }

  function canProceed(): boolean {
    switch (step) {
      case 0:
        return state.kbli_code.trim().length > 0 && state.kbli_description.trim().length > 0;
      case 1:
        return state.business_scale !== "" && state.risk_level !== "";
      case 2:
        return true;
      case 3:
        return true;
      default:
        return false;
    }
  }

  async function handleSubmit() {
    setIsSubmitting(true);
    setError(null);
    try {
      const payload: PermitApplyPayload = {
        kbli_code: state.kbli_code,
        kbli_description: state.kbli_description,
        kbli_section: state.kbli_section || undefined,
        business_scale: state.business_scale as BusinessScale,
        risk_level: state.risk_level as RiskLevel,
        business_location_province: state.business_location_province || undefined,
        business_location_city: state.business_location_city || undefined,
        business_location_address: state.business_location_address || undefined,
        annual_revenue_estimate: state.annual_revenue_estimate
          ? Number(state.annual_revenue_estimate)
          : undefined,
        employee_count: state.employee_count
          ? Number(state.employee_count)
          : undefined,
        applicant_notes: state.applicant_notes || undefined,
      };

      const result = await applyPermit(payload);
      setSubmitted({ application_number: result.application_number });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Terjadi kesalahan");
    } finally {
      setIsSubmitting(false);
    }
  }

  if (submitted) {
    return (
      <div className="flex flex-col items-center py-16 text-center">
        <div className="mb-4 flex h-20 w-20 items-center justify-center rounded-2xl bg-green-100">
          <CheckCircle2 className="h-10 w-10 text-green-500" />
        </div>
        <h3 className="mb-2 text-xl font-bold text-gray-900">
          Permohonan Berhasil Dikirim!
        </h3>
        <p className="mb-1 text-sm text-gray-500">Nomor permohonan Anda:</p>
        <p className="mb-6 font-mono text-lg font-bold text-brand-700">
          {submitted.application_number}
        </p>
        <p className="mb-8 max-w-sm text-sm leading-relaxed text-gray-500">
          Tim DPMPTSP akan meninjau permohonan Anda. Anda akan mendapat notifikasi
          melalui WhatsApp.
        </p>
        <button
          onClick={() => router.push("/permits")}
          className="rounded-xl bg-brand-700 px-6 py-2.5 text-sm font-semibold text-white hover:bg-brand-800"
        >
          Lihat Perizinan Saya
        </button>
      </div>
    );
  }

  return (
    <div>
      <StepIndicator current={step} />

      <div className="animate-slide-up">
        {step === 0 && <Step1 state={state} update={update} />}
        {step === 1 && <Step2 state={state} update={update} />}
        {step === 2 && <Step3 state={state} update={update} />}
        {step === 3 && <Step4 state={state} />}
      </div>

      {error && (
        <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Navigation buttons */}
      <div className="mt-8 flex items-center justify-between">
        <button
          type="button"
          onClick={() => setStep((s) => Math.max(0, s - 1))}
          disabled={step === 0}
          className={cn(
            "flex items-center gap-2 rounded-xl px-5 py-2.5 text-sm font-medium transition-colors",
            step === 0
              ? "invisible"
              : "border border-gray-200 text-gray-700 hover:bg-gray-50",
          )}
        >
          <ArrowLeft className="h-4 w-4" />
          Sebelumnya
        </button>

        {step < STEPS.length - 1 ? (
          <button
            type="button"
            onClick={() => setStep((s) => s + 1)}
            disabled={!canProceed()}
            className={cn(
              "flex items-center gap-2 rounded-xl px-5 py-2.5 text-sm font-semibold transition-colors",
              canProceed()
                ? "bg-brand-700 text-white hover:bg-brand-800"
                : "cursor-not-allowed bg-gray-100 text-gray-400",
            )}
          >
            Selanjutnya
            <ArrowRight className="h-4 w-4" />
          </button>
        ) : (
          <button
            type="button"
            onClick={handleSubmit}
            disabled={isSubmitting}
            className="flex items-center gap-2 rounded-xl bg-green-600 px-6 py-2.5 text-sm font-semibold text-white hover:bg-green-700 disabled:opacity-70"
          >
            {isSubmitting ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <CheckCircle2 className="h-4 w-4" />
            )}
            {isSubmitting ? "Mengirim..." : "Kirim Permohonan"}
          </button>
        )}
      </div>
    </div>
  );
}
