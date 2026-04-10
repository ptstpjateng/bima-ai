"use client";

import {
  Building2,
  Check,
  CheckCircle2,
  ExternalLink,
  Loader2,
  Mail,
  MapPin,
  Pencil,
  Phone,
  Send,
  Shield,
  User,
  X,
} from "lucide-react";
import { useState } from "react";
import { AppLayout } from "@/components/layout/AppLayout";
import { useAuth } from "@/context/AuthContext";
import { Skeleton } from "@/components/shared/LoadingSkeleton";
import { updateProfile, generateTelegramToken } from "@/lib/api";
import type { ProfileUpdatePayload } from "@/lib/api";
import { cn } from "@/lib/utils";

const TELEGRAM_BOT = "bima_ai_bot";

function ProfileField({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: string | null | undefined;
  icon?: React.ElementType;
}) {
  return (
    <div className="flex items-start gap-3 border-b border-gray-50 py-3.5 last:border-0">
      {Icon && (
        <div className="mt-0.5 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-gray-100">
          <Icon className="h-4 w-4 text-gray-500" />
        </div>
      )}
      <div className="flex-1 min-w-0">
        <p className="text-xs font-medium text-gray-400">{label}</p>
        <p className="mt-0.5 text-sm font-medium text-gray-900">
          {value || <span className="text-gray-400 font-normal">Belum diisi</span>}
        </p>
      </div>
    </div>
  );
}

function EditField({
  label,
  name,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  name: keyof ProfileUpdatePayload;
  value: string;
  onChange: (name: keyof ProfileUpdatePayload, val: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <div className="py-3 border-b border-gray-50 last:border-0">
      <label className="text-xs font-medium text-gray-400">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(name, e.target.value)}
        placeholder={placeholder ?? label}
        className="mt-1 w-full rounded-xl border border-gray-200 px-3 py-2 text-sm text-gray-900 outline-none transition focus:border-brand-400 focus:ring-2 focus:ring-brand-100 placeholder:text-gray-300"
      />
    </div>
  );
}

export default function ProfilePage() {
  const { user, updateUser } = useAuth();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [form, setForm] = useState<ProfileUpdatePayload>({});

  // Telegram linking state
  const [tgLoading, setTgLoading] = useState(false);
  const [tgToken, setTgToken] = useState<string | null>(null);
  const [tgError, setTgError] = useState<string | null>(null);

  function startEdit() {
    if (!user) return;
    setForm({
      name: user.name ?? "",
      phone: user.phone ?? "",
      nik: user.nik ?? "",
      npwp: user.npwp ?? "",
      business_name: user.business_name ?? "",
      business_address: user.business_address ?? "",
    });
    setSaveError(null);
    setEditing(true);
  }

  function handleChange(name: keyof ProfileUpdatePayload, val: string) {
    setForm((prev) => ({ ...prev, [name]: val }));
  }

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    try {
      const updated = await updateProfile(form);
      updateUser(updated);
      setEditing(false);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Gagal menyimpan. Coba lagi.");
    } finally {
      setSaving(false);
    }
  }

  async function handleTelegramConnect() {
    setTgLoading(true);
    setTgError(null);
    setTgToken(null);
    try {
      const token = await generateTelegramToken();
      setTgToken(token);
    } catch (err) {
      setTgError(err instanceof Error ? err.message : "Gagal membuat token.");
    } finally {
      setTgLoading(false);
    }
  }

  const telegramDeepLink = tgToken
    ? `https://t.me/${TELEGRAM_BOT}?start=tglink_${tgToken}`
    : null;

  return (
    <AppLayout>
      <div className="mx-auto max-w-2xl space-y-5">
        {/* Header */}
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-bold text-gray-900">Profil & Data Usaha</h1>
            <p className="text-sm text-gray-500">
              Kelola informasi akun dan data usaha Anda
            </p>
          </div>
          {user && !editing && (
            <button
              type="button"
              onClick={startEdit}
              className="inline-flex items-center gap-1.5 rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-700 shadow-sm hover:bg-gray-50 transition-colors"
            >
              <Pencil className="h-4 w-4" />
              Edit
            </button>
          )}
        </div>

        {!user ? (
          <div className="space-y-5">
            <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
              <Skeleton className="mb-4 h-16 w-16 rounded-full" />
              <Skeleton className="mb-2 h-5 w-1/3" />
              <Skeleton className="h-4 w-1/4" />
            </div>
          </div>
        ) : (
          <>
            {/* Avatar section */}
            <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
              <div className="flex items-center gap-4">
                <div className="flex h-16 w-16 flex-shrink-0 items-center justify-center rounded-2xl bg-brand-100 text-2xl font-bold text-brand-700">
                  {user.name.charAt(0).toUpperCase()}
                </div>
                <div>
                  <h2 className="text-lg font-bold text-gray-900">{user.name}</h2>
                  <p className="text-sm text-gray-500">{user.email}</p>
                  <span className="mt-1 inline-flex items-center gap-1 rounded-full bg-brand-50 px-2.5 py-0.5 text-xs font-medium text-brand-700">
                    <Shield className="h-3 w-3" />
                    {user.role === "msme"
                      ? "Pelaku UMKM"
                      : user.role === "staff"
                        ? "Petugas DPMPTSP"
                        : "Administrator"}
                  </span>
                </div>
              </div>
            </div>

            {/* Personal info */}
            <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
              <div className="mb-4 flex items-center gap-2">
                <User className="h-4 w-4 text-gray-400" />
                <h3 className="text-sm font-bold text-gray-900">Informasi Pribadi</h3>
              </div>

              {editing ? (
                <>
                  <EditField label="Nama Lengkap" name="name" value={form.name ?? ""} onChange={handleChange} />
                  <EditField label="Nomor WhatsApp / Telepon" name="phone" value={form.phone ?? ""} onChange={handleChange} type="tel" placeholder="08xxxxxxxxxx" />
                  <EditField label="NIK (16 digit)" name="nik" value={form.nik ?? ""} onChange={handleChange} placeholder="3300000000000000" />
                  <EditField label="NPWP" name="npwp" value={form.npwp ?? ""} onChange={handleChange} placeholder="00.000.000.0-000.000" />
                </>
              ) : (
                <>
                  <ProfileField label="Nama Lengkap" value={user.name} icon={User} />
                  <ProfileField label="Email" value={user.email} icon={Mail} />
                  <ProfileField label="Nomor WhatsApp / Telepon" value={user.phone} icon={Phone} />
                  <ProfileField label="NIK" value={user.nik} />
                  <ProfileField label="NPWP" value={user.npwp} />
                </>
              )}
            </div>

            {/* Business info */}
            <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
              <div className="mb-4 flex items-center gap-2">
                <Building2 className="h-4 w-4 text-gray-400" />
                <h3 className="text-sm font-bold text-gray-900">Data Usaha</h3>
              </div>

              {editing ? (
                <>
                  <EditField label="Nama Usaha" name="business_name" value={form.business_name ?? ""} onChange={handleChange} placeholder="CV / PT / UD ..." />
                  <EditField label="Alamat Usaha" name="business_address" value={form.business_address ?? ""} onChange={handleChange} />
                </>
              ) : (
                <>
                  <ProfileField label="Nama Usaha" value={user.business_name} icon={Building2} />
                  <ProfileField label="Bentuk Badan Usaha" value={user.business_legal_type} />
                  <ProfileField label="Alamat Usaha" value={user.business_address} icon={MapPin} />
                </>
              )}
            </div>

            {/* Address (read-only — set by DPMPTSP) */}
            {!editing && (user.province || user.city) && (
              <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
                <div className="mb-4 flex items-center gap-2">
                  <MapPin className="h-4 w-4 text-gray-400" />
                  <h3 className="text-sm font-bold text-gray-900">Alamat</h3>
                </div>
                <ProfileField label="Provinsi" value={user.province} />
                <ProfileField label="Kota / Kabupaten" value={user.city} />
                <ProfileField label="Kecamatan" value={user.district} />
                <ProfileField label="Kelurahan / Desa" value={user.village} />
                <ProfileField label="Kode Pos" value={user.postal_code} />
              </div>
            )}

            {/* Edit mode save/cancel bar */}
            {editing && (
              <div className="rounded-2xl border border-gray-100 bg-white p-4 shadow-sm">
                {saveError && (
                  <p className="mb-3 rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700">
                    {saveError}
                  </p>
                )}
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={handleSave}
                    disabled={saving}
                    className={cn(
                      "inline-flex flex-1 items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-semibold text-white transition-colors",
                      saving ? "bg-brand-400 cursor-not-allowed" : "bg-brand-700 hover:bg-brand-800",
                    )}
                  >
                    {saving ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Check className="h-4 w-4" />
                    )}
                    {saving ? "Menyimpan..." : "Simpan Perubahan"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditing(false)}
                    disabled={saving}
                    className="inline-flex items-center gap-1.5 rounded-xl border border-gray-200 px-4 py-2.5 text-sm font-medium text-gray-700 hover:bg-gray-50 transition-colors"
                  >
                    <X className="h-4 w-4" />
                    Batal
                  </button>
                </div>
              </div>
            )}

            {/* Telegram connect */}
            <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
              <div className="mb-4 flex items-center gap-2">
                <Send className="h-4 w-4 text-gray-400" />
                <h3 className="text-sm font-bold text-gray-900">Hubungkan Telegram</h3>
              </div>

              {user.telegram_chat_id ? (
                <div className="flex items-center gap-3 rounded-xl border border-green-200 bg-green-50 px-4 py-3">
                  <CheckCircle2 className="h-5 w-5 flex-shrink-0 text-green-500" />
                  <div>
                    <p className="text-sm font-semibold text-green-800">Telegram terhubung</p>
                    {user.telegram_username && (
                      <p className="text-xs text-green-700">@{user.telegram_username}</p>
                    )}
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <p className="text-sm text-gray-500 leading-relaxed">
                    Hubungkan akun Telegram Anda agar BIMA-AI bisa mengirim notifikasi
                    status izin, pengingat LKPM, dan menjawab pertanyaan Anda langsung
                    via Telegram.
                  </p>

                  {tgError && (
                    <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-700">
                      {tgError}
                    </p>
                  )}

                  {telegramDeepLink ? (
                    <div className="rounded-xl border border-brand-200 bg-brand-50 p-4">
                      <p className="mb-3 text-xs font-semibold text-brand-800">
                        Token siap (berlaku 15 menit)
                      </p>
                      <p className="mb-3 text-xs text-brand-700 leading-relaxed">
                        Klik tombol di bawah untuk membuka Telegram dan ketuk{" "}
                        <strong>Start</strong> — akun Anda akan terhubung otomatis.
                      </p>
                      <a
                        href={telegramDeepLink}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 rounded-xl bg-brand-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 transition-colors"
                      >
                        <Send className="h-4 w-4" />
                        Buka @bima_ai_bot di Telegram
                        <ExternalLink className="h-3.5 w-3.5" />
                      </a>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={handleTelegramConnect}
                      disabled={tgLoading}
                      className="inline-flex items-center gap-2 rounded-xl bg-brand-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-800 transition-colors disabled:opacity-60"
                    >
                      {tgLoading ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Send className="h-4 w-4" />
                      )}
                      {tgLoading ? "Membuat token..." : "Hubungkan Telegram"}
                    </button>
                  )}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </AppLayout>
  );
}
