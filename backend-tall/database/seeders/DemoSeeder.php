<?php

namespace Database\Seeders;

use App\Models\Business;
use App\Models\PermitApplication;
use App\Models\User;
use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * Seeds the hackathon demo account.
 *
 * Run with:
 *   php artisan db:seed --class=DemoSeeder
 *
 * Creates:
 *   - demo@bima.ai — MSME user, full profile, Telegram-linked
 *   - admin@bima.ai — admin/DPMPTSP staff account
 *   - 1 approved permit (KBLI 56102 Warung Makan)
 *   - 1 submitted permit (KBLI 47221 Toko Sembako) — shows "under review" state
 *   - Linked business record
 */
class DemoSeeder extends Seeder
{
    public function run(): void
    {
        DB::transaction(function () {
            // ── Admin account ───────────────────────────────────────────────
            $admin = User::updateOrCreate(
                ['email' => 'admin@bima.ai'],
                [
                    'name'              => 'Admin DPMPTSP',
                    'password'          => 'demo1234',   // hashed by model cast
                    'role'              => 'admin',
                    'email_verified_at' => now(),
                ]
            );

            // ── Demo MSME user ──────────────────────────────────────────────
            $demo = User::updateOrCreate(
                ['email' => 'demo@bima.ai'],
                [
                    'name'                 => 'Budi Santoso',
                    'password'             => 'demo1234',
                    'role'                 => 'msme',
                    'email_verified_at'    => now(),
                    'phone'                => '081234567890',
                    'nik'                  => '3374012501900001',
                    'npwp'                 => '12.345.678.9-012.000',
                    'business_name'        => 'Warung Makan Bu Sari',
                    'business_legal_type'  => 'Perorangan',
                    'business_address'     => 'Jl. Semarang Indah No. 12, Semarang Barat',
                    'province'             => 'Jawa Tengah',
                    'city'                 => 'Semarang',
                    'district'             => 'Semarang Barat',
                    'village'              => 'Manyaran',
                    'postal_code'          => '50148',
                ]
            );

            // ── Business record ─────────────────────────────────────────────
            Business::updateOrCreate(
                ['user_id' => $demo->id, 'is_primary' => true],
                [
                    'name'                      => 'Warung Makan Bu Sari',
                    'primary_kbli_code'         => '56102',
                    'primary_kbli_description'  => 'Warung/Kedai Makan',
                    'legal_type'                => 'Perorangan',
                    'scale'                     => 'kecil',
                    'annual_revenue_estimate'   => 450_000_000,  // Rp 450 juta/year
                    'employee_count'            => 5,
                    'address'                   => 'Jl. Semarang Indah No. 12, Semarang Barat',
                ]
            );

            // ── Approved permit (Phase 2 demo pivot point) ──────────────────
            PermitApplication::updateOrCreate(
                [
                    'user_id'            => $demo->id,
                    'application_number' => 'BIMA-2026-001',
                ],
                [
                    'kbli_code'                 => '56102',
                    'kbli_description'          => 'Warung/Kedai Makan',
                    'kbli_section'              => 'Penyediaan Akomodasi dan Penyediaan Makan Minum',
                    'business_scale'            => 'kecil',
                    'risk_level'                => 'menengah_rendah',
                    'status'                    => 'approved',
                    'business_location_address' => 'Jl. Semarang Indah No. 12, Semarang Barat',
                    'business_location_province'=> 'Jawa Tengah',
                    'business_location_city'    => 'Semarang',
                    'annual_revenue_estimate'   => 450_000_000,
                    'employee_count'            => 5,
                    'applicant_notes'           => 'Warung makan sederhana, buka setiap hari 07.00–21.00.',
                    'reviewer_notes'            => 'Semua persyaratan terpenuhi. Usaha memenuhi kriteria skala kecil. Disetujui.',
                    'reviewer_id'               => $admin->id,
                    'submitted_at'              => now()->subDays(14),
                    'reviewed_at'               => now()->subDays(10),
                    'approved_at'               => now()->subDays(10),
                    'permit_expiry_date'        => now()->addYears(3)->toDateString(),
                    'documents' => [
                        [
                            'path'  => 'https://example.com/docs/ktp-budi.pdf',
                            'type'  => 'ktp',
                            'notes' => 'KTP atas nama Budi Santoso',
                        ],
                        [
                            'path'  => 'https://example.com/docs/npwp-budi.pdf',
                            'type'  => 'npwp',
                            'notes' => null,
                        ],
                        [
                            'path'  => 'https://example.com/docs/foto-tempat-budi.pdf',
                            'type'  => 'foto_tempat_usaha',
                            'notes' => 'Foto tampak depan dan dalam',
                        ],
                    ],
                    'requirements_checklist' => [
                        ['label' => 'KTP Pemohon', 'met' => true, 'notes' => null],
                        ['label' => 'NPWP', 'met' => true, 'notes' => null],
                        ['label' => 'Foto Tempat Usaha', 'met' => true, 'notes' => null],
                        ['label' => 'Surat Pernyataan Usaha', 'met' => true, 'notes' => null],
                        ['label' => 'Izin Gangguan (HO)', 'met' => true, 'notes' => 'Skala kecil — tidak diperlukan HO terpisah'],
                    ],
                ]
            );

            // ── Submitted permit (shows "under review" lifecycle) ───────────
            PermitApplication::updateOrCreate(
                [
                    'user_id'            => $demo->id,
                    'application_number' => 'BIMA-2026-002',
                ],
                [
                    'kbli_code'                 => '47221',
                    'kbli_description'          => 'Perdagangan Eceran Beras, Tepung dan Produk Sejenisnya',
                    'kbli_section'              => 'Perdagangan Besar dan Eceran',
                    'business_scale'            => 'mikro',
                    'risk_level'                => 'rendah',
                    'status'                    => 'under_review',
                    'business_location_address' => 'Jl. Semarang Indah No. 14, Semarang Barat',
                    'business_location_province'=> 'Jawa Tengah',
                    'business_location_city'    => 'Semarang',
                    'annual_revenue_estimate'   => 120_000_000,
                    'employee_count'            => 2,
                    'applicant_notes'           => 'Toko sembako kecil, berdampingan dengan warung makan.',
                    'reviewer_notes'            => null,
                    'submitted_at'              => now()->subDays(3),
                    'documents' => [
                        [
                            'path'  => 'https://example.com/docs/ktp-budi-2.pdf',
                            'type'  => 'ktp',
                            'notes' => null,
                        ],
                    ],
                    'requirements_checklist' => [
                        ['label' => 'KTP Pemohon', 'met' => true, 'notes' => null],
                        ['label' => 'NPWP', 'met' => false, 'notes' => 'Menunggu verifikasi NPWP'],
                    ],
                ]
            );
        });

        $this->command->info('Demo seeder completed:');
        $this->command->info('  admin@bima.ai  — password: demo1234  (Filament admin)');
        $this->command->info('  demo@bima.ai   — password: demo1234  (MSME portal user)');
        $this->command->info('  2 permit applications seeded (1 approved, 1 under_review)');
    }
}
