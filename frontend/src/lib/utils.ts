import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import type {
  BusinessScale,
  PermitStatus,
  RiskLevel,
} from "@/types";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(dateString: string | null | undefined): string {
  if (!dateString) return "-";
  return new Intl.DateTimeFormat("id-ID", {
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(new Date(dateString));
}

export function formatDateTime(dateString: string | null | undefined): string {
  if (!dateString) return "-";
  return new Intl.DateTimeFormat("id-ID", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(dateString));
}

export function formatCurrency(amount: number | null | undefined): string {
  if (amount == null) return "-";
  return new Intl.NumberFormat("id-ID", {
    style: "currency",
    currency: "IDR",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(amount);
}

export function formatRelativeTime(dateString: string | null | undefined): string {
  if (!dateString) return "-";
  const date = new Date(dateString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMins < 1) return "Baru saja";
  if (diffMins < 60) return `${diffMins} menit lalu`;
  if (diffHours < 24) return `${diffHours} jam lalu`;
  if (diffDays < 7) return `${diffDays} hari lalu`;
  return formatDate(dateString);
}

export const STATUS_LABELS: Record<PermitStatus, string> = {
  draft: "Draft",
  submitted: "Dikirim",
  under_review: "Sedang Ditinjau",
  additional_docs_required: "Dokumen Tambahan",
  approved: "Disetujui",
  rejected: "Ditolak",
  expired: "Kadaluarsa",
};

export const STATUS_COLORS: Record<PermitStatus, string> = {
  draft: "bg-gray-100 text-gray-700 border-gray-200",
  submitted: "bg-blue-50 text-blue-700 border-blue-200",
  under_review: "bg-yellow-50 text-yellow-700 border-yellow-200",
  additional_docs_required: "bg-orange-50 text-orange-700 border-orange-200",
  approved: "bg-green-50 text-green-700 border-green-200",
  rejected: "bg-red-50 text-red-700 border-red-200",
  expired: "bg-gray-50 text-gray-500 border-gray-200",
};

export const SCALE_LABELS: Record<BusinessScale, string> = {
  mikro: "Usaha Mikro",
  kecil: "Usaha Kecil",
  menengah: "Usaha Menengah",
  besar: "Usaha Besar",
};

export const RISK_LABELS: Record<RiskLevel, string> = {
  rendah: "Risiko Rendah",
  menengah_rendah: "Risiko Menengah Rendah",
  menengah_tinggi: "Risiko Menengah Tinggi",
  tinggi: "Risiko Tinggi",
};

export const RISK_COLORS: Record<RiskLevel, string> = {
  rendah: "text-green-700 bg-green-50",
  menengah_rendah: "text-yellow-700 bg-yellow-50",
  menengah_tinggi: "text-orange-700 bg-orange-50",
  tinggi: "text-red-700 bg-red-50",
};

export function getGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 11) return "Selamat pagi";
  if (hour < 15) return "Selamat siang";
  if (hour < 18) return "Selamat sore";
  return "Selamat malam";
}
