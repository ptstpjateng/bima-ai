"use client";

import {
  Building2,
  Mail,
  MapPin,
  Phone,
  Shield,
  User,
} from "lucide-react";
import { AppLayout } from "@/components/layout/AppLayout";
import { useAuth } from "@/context/AuthContext";
import { Skeleton } from "@/components/shared/LoadingSkeleton";

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

export default function ProfilePage() {
  const { user } = useAuth();

  return (
    <AppLayout>
      <div className="mx-auto max-w-2xl space-y-5">
        {/* Header */}
        <div>
          <h1 className="text-xl font-bold text-gray-900">Profil & Data Usaha</h1>
          <p className="text-sm text-gray-500">
            Data profil Anda yang terdaftar di sistem BIMA-AI
          </p>
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
                <h3 className="text-sm font-bold text-gray-900">
                  Informasi Pribadi
                </h3>
              </div>
              <ProfileField label="Nama Lengkap" value={user.name} icon={User} />
              <ProfileField label="Email" value={user.email} icon={Mail} />
              <ProfileField
                label="Nomor WhatsApp / Telepon"
                value={user.phone}
                icon={Phone}
              />
              <ProfileField label="NIK" value={user.nik} />
              <ProfileField label="NPWP" value={user.npwp} />
            </div>

            {/* Business info */}
            <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
              <div className="mb-4 flex items-center gap-2">
                <Building2 className="h-4 w-4 text-gray-400" />
                <h3 className="text-sm font-bold text-gray-900">
                  Data Usaha
                </h3>
              </div>
              <ProfileField
                label="Nama Usaha"
                value={user.business_name}
                icon={Building2}
              />
              <ProfileField
                label="Bentuk Badan Usaha"
                value={user.business_legal_type}
              />
              <ProfileField
                label="Alamat Usaha"
                value={user.business_address}
                icon={MapPin}
              />
            </div>

            {/* Address */}
            {(user.province || user.city) && (
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

            {/* Notice */}
            <div className="rounded-2xl border border-brand-100 bg-brand-50 p-4">
              <p className="text-xs font-semibold text-brand-800">
                ℹ️ Perbarui Data
              </p>
              <p className="mt-1 text-xs text-brand-700 leading-relaxed">
                Untuk memperbarui data profil atau data usaha, hubungi petugas
                DPMPTSP atau konsultasikan via WhatsApp dengan asisten BIMA-AI.
              </p>
            </div>
          </>
        )}
      </div>
    </AppLayout>
  );
}
