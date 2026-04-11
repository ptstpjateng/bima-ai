<x-filament-panels::page class="data-import-hub">
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
        {{-- KBLI Stats --}}
        <x-filament::section>
            <x-slot name="heading">Data KBLI</x-slot>
            <x-slot name="description">Klasifikasi Baku Lapangan Usaha Indonesia</x-slot>
            <div class="space-y-3">
                <div class="flex justify-between items-center">
                    <span class="text-sm opacity-70">Total Record</span>
                    <span class="text-2xl font-extrabold" style="color: var(--va-primary, #fbbf24);">
                        {{ \App\Models\Kbli::count() }}
                    </span>
                </div>
                <div class="flex justify-between items-center">
                    <span class="text-sm opacity-70">Kode KBLI Unik</span>
                    <span class="text-lg font-bold opacity-80">
                        {{ \App\Models\Kbli::distinct('kode_kbli')->count('kode_kbli') }}
                    </span>
                </div>
                <div class="flex justify-between items-center">
                    <span class="text-sm opacity-70">Terakhir Diperbarui</span>
                    <span class="text-sm opacity-60">
                        {{ \App\Models\Kbli::latest('updated_at')->value('updated_at')?->diffForHumans() ?? '—' }}
                    </span>
                </div>
            </div>
        </x-filament::section>

        {{-- PB UMKU Stats --}}
        <x-filament::section>
            <x-slot name="heading">Data PB UMKU</x-slot>
            <x-slot name="description">Perizinan Berusaha Untuk Menunjang Kegiatan Usaha</x-slot>
            <div class="space-y-3">
                <div class="flex justify-between items-center">
                    <span class="text-sm opacity-70">Total Record</span>
                    <span class="text-2xl font-extrabold" style="color: var(--va-primary, #fbbf24);">
                        {{ \App\Models\PbUmku::count() }}
                    </span>
                </div>
                <div class="flex justify-between items-center">
                    <span class="text-sm opacity-70">Nomeklatur Unik</span>
                    <span class="text-lg font-bold opacity-80">
                        {{ \App\Models\PbUmku::distinct('nomeklatur')->count('nomeklatur') }}
                    </span>
                </div>
                <div class="flex justify-between items-center">
                    <span class="text-sm opacity-70">Terakhir Diperbarui</span>
                    <span class="text-sm opacity-60">
                        {{ \App\Models\PbUmku::latest('updated_at')->value('updated_at')?->diffForHumans() ?? '—' }}
                    </span>
                </div>
            </div>
        </x-filament::section>

        {{-- ChromaDB Status --}}
        <x-filament::section>
            <x-slot name="heading">ChromaDB RAG</x-slot>
            <x-slot name="description">Vector store untuk AI Engine</x-slot>
            <div class="space-y-3">
                <div class="flex justify-between items-center">
                    <span class="text-sm opacity-70">Status</span>
                    <x-filament::badge color="success">Active</x-filament::badge>
                </div>
                <div class="flex justify-between items-center">
                    <span class="text-sm opacity-70">Collection</span>
                    <span class="text-sm font-mono opacity-80">oss_regulations</span>
                </div>
                <p class="text-xs opacity-50 pt-2">
                    Setelah mengimpor data Excel, klik "Sync ke ChromaDB" untuk memperbarui vector store.
                </p>
            </div>
        </x-filament::section>
    </div>

    <x-filament::section class="mt-6">
        <x-slot name="heading">Panduan Import</x-slot>
        <div class="prose prose-sm prose-invert max-w-none opacity-80">
            <ol>
                <li><strong>Upload KBLI Excel</strong> — File <code>kbli.xlsx</code> dengan kolom: No, Kode KBLI, Judul KBLI, Ruang Lingkup, Skala Usaha, Tingkat Risiko, Perizinan Berusaha, Persyaratan, Jangka Waktu Penerbitan, Kewajiban, PB UMKU, Parameter, Kewenangan.</li>
                <li><strong>Upload PB UMKU Excel</strong> — File <code>pb umku.xlsx</code> dengan kolom: No, Nomeklatur PB UMKU, Persyaratan, Jangka Waktu Penerbitan, Kewajiban, Masa Berlaku, Parameter, Kewenangan.</li>
                <li><strong>Sync ke ChromaDB</strong> — Membangun ulang vector embeddings dari data PostgreSQL untuk RAG retrieval AI Engine.</li>
            </ol>
            <p><em>Upload akan menghapus data lama dan menggantinya sepenuhnya dengan data dari file Excel baru.</em></p>
        </div>
    </x-filament::section>
</x-filament-panels::page>
