import { AppLayout } from "@/components/layout/AppLayout";
import { ApplyWizard } from "@/components/permits/ApplyWizard";

export default function ApplyPage() {
  return (
    <AppLayout>
      <div className="mx-auto max-w-2xl">
        {/* Page header */}
        <div className="mb-6">
          <h1 className="text-xl font-bold text-gray-900">
            Ajukan Perizinan Baru
          </h1>
          <p className="text-sm text-gray-500">
            Ikuti panduan berikut untuk mengajukan permohonan izin usaha OSS RBA
          </p>
        </div>

        {/* Wizard card */}
        <div className="rounded-2xl border border-gray-100 bg-white p-6 shadow-sm">
          <ApplyWizard />
        </div>
      </div>
    </AppLayout>
  );
}
