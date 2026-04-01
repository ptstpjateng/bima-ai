<?php

namespace Database\Seeders;

use App\Models\KbliScrapeTarget;
use Illuminate\Database\Seeder;

class KbliScrapeTargetSeeder extends Seeder
{
    /**
     * 35 KBLI codes curated for DPMPTSP Jawa Tengah MSME context.
     * Mirrors the initial run_full_pipeline.py list so statuses can be tracked.
     */
    private const TARGETS = [
        // Food & Beverage
        ['kbli_code' => '56101', 'kbli_description' => 'Restoran',                          'sector' => 'Food & Beverage',      'priority' => 10],
        ['kbli_code' => '56102', 'kbli_description' => 'Warung / Rumah Makan',               'sector' => 'Food & Beverage',      'priority' => 10],
        ['kbli_code' => '56103', 'kbli_description' => 'Penyediaan Makanan Keliling',        'sector' => 'Food & Beverage',      'priority' => 8],
        ['kbli_code' => '10710', 'kbli_description' => 'Industri Produk Roti & Kue',        'sector' => 'Food & Beverage',      'priority' => 6],
        ['kbli_code' => '10750', 'kbli_description' => 'Industri Makanan & Masakan Olahan', 'sector' => 'Food & Beverage',      'priority' => 5],
        ['kbli_code' => '56301', 'kbli_description' => 'Bar',                               'sector' => 'Food & Beverage',      'priority' => 3],
        // Retail & Trade
        ['kbli_code' => '47111', 'kbli_description' => 'Minimarket (s.d. 400 m²)',          'sector' => 'Retail & Trade',       'priority' => 9],
        ['kbli_code' => '47112', 'kbli_description' => 'Supermarket (400–5.000 m²)',        'sector' => 'Retail & Trade',       'priority' => 7],
        ['kbli_code' => '47193', 'kbli_description' => 'Toko Kelontong',                    'sector' => 'Retail & Trade',       'priority' => 8],
        ['kbli_code' => '47741', 'kbli_description' => 'Apotek',                            'sector' => 'Retail & Trade',       'priority' => 9],
        ['kbli_code' => '47742', 'kbli_description' => 'Toko Obat',                         'sector' => 'Retail & Trade',       'priority' => 7],
        ['kbli_code' => '47721', 'kbli_description' => 'Toko Pakaian',                      'sector' => 'Retail & Trade',       'priority' => 6],
        ['kbli_code' => '47591', 'kbli_description' => 'Toko Furnitur & Perlengkapan Rumah','sector' => 'Retail & Trade',       'priority' => 4],
        // Health Services
        ['kbli_code' => '86101', 'kbli_description' => 'Aktivitas RS Umum Pemerintah',      'sector' => 'Health Services',      'priority' => 8],
        ['kbli_code' => '86210', 'kbli_description' => 'Praktik Dokter Umum',               'sector' => 'Health Services',      'priority' => 9],
        ['kbli_code' => '86901', 'kbli_description' => 'Praktik Dokter Gigi',               'sector' => 'Health Services',      'priority' => 8],
        ['kbli_code' => '86903', 'kbli_description' => 'Pelayanan Kesehatan Lainnya',       'sector' => 'Health Services',      'priority' => 6],
        // IT & Technology
        ['kbli_code' => '62010', 'kbli_description' => 'Aktivitas Pemrograman Komputer',    'sector' => 'IT & Technology',      'priority' => 8],
        ['kbli_code' => '62019', 'kbli_description' => 'Aktivitas Konsultasi TIK Lainnya', 'sector' => 'IT & Technology',      'priority' => 7],
        ['kbli_code' => '62020', 'kbli_description' => 'Konsultasi Manajemen TIK',          'sector' => 'IT & Technology',      'priority' => 6],
        ['kbli_code' => '63110', 'kbli_description' => 'Pengolahan Data & Hosting Web',     'sector' => 'IT & Technology',      'priority' => 6],
        // Construction
        ['kbli_code' => '43211', 'kbli_description' => 'Instalasi Listrik',                 'sector' => 'Construction',         'priority' => 7],
        ['kbli_code' => '43221', 'kbli_description' => 'Instalasi Pipa & Sanitasi',         'sector' => 'Construction',         'priority' => 6],
        ['kbli_code' => '41011', 'kbli_description' => 'Konstruksi Gedung Hunian',          'sector' => 'Construction',         'priority' => 5],
        // Beauty & Personal Care
        ['kbli_code' => '96021', 'kbli_description' => 'Salon Kecantikan',                  'sector' => 'Beauty & Personal',    'priority' => 8],
        ['kbli_code' => '96022', 'kbli_description' => 'Pangkas Rambut (Barbershop)',        'sector' => 'Beauty & Personal',    'priority' => 7],
        // Education & Training
        ['kbli_code' => '85491', 'kbli_description' => 'Kursus Bahasa',                     'sector' => 'Education & Training', 'priority' => 5],
        ['kbli_code' => '85499', 'kbli_description' => 'Pendidikan Lainnya',                'sector' => 'Education & Training', 'priority' => 5],
        // Transportation & Logistics
        ['kbli_code' => '49310', 'kbli_description' => 'Angkutan Darat (Penumpang)',         'sector' => 'Transportation',       'priority' => 5],
        ['kbli_code' => '52291', 'kbli_description' => 'Jasa Pengurusan Transportasi',      'sector' => 'Transportation',       'priority' => 4],
        // Manufacturing
        ['kbli_code' => '13910', 'kbli_description' => 'Industri Rajut & Sulaman',          'sector' => 'Manufacturing',        'priority' => 4],
        ['kbli_code' => '22192', 'kbli_description' => 'Industri Produk Karet Lainnya',     'sector' => 'Manufacturing',        'priority' => 3],
        // Accommodation
        ['kbli_code' => '55110', 'kbli_description' => 'Hotel Bintang',                     'sector' => 'Accommodation',        'priority' => 6],
        ['kbli_code' => '55120', 'kbli_description' => 'Hotel Melati',                      'sector' => 'Accommodation',        'priority' => 7],
        // Agriculture
        ['kbli_code' => '01111', 'kbli_description' => 'Pertanian Padi',                    'sector' => 'Agriculture',          'priority' => 4],
    ];

    public function run(): void
    {
        foreach (self::TARGETS as $target) {
            KbliScrapeTarget::updateOrCreate(
                ['kbli_code' => $target['kbli_code']],
                array_merge($target, ['status' => 'pending'])
            );
        }

        $this->command->info('Seeded ' . count(self::TARGETS) . ' KBLI scrape targets.');
    }
}
