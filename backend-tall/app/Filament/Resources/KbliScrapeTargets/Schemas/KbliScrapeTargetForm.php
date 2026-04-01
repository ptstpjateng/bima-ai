<?php

namespace App\Filament\Resources\KbliScrapeTargets\Schemas;

use Filament\Forms\Components\Section;
use Filament\Forms\Components\Select;
use Filament\Forms\Components\TextInput;
use Filament\Schemas\Schema;

class KbliScrapeTargetForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema->components([

            Section::make('Identitas KBLI')
                ->columns(2)
                ->schema([
                    TextInput::make('kbli_code')
                        ->label('Kode KBLI')
                        ->required()
                        ->maxLength(10)
                        ->placeholder('56102')
                        ->helperText('Kode 5 digit dari sistem OSS, e.g. 56102, 62019')
                        ->unique(ignoreRecord: true),

                    TextInput::make('kbli_description')
                        ->label('Deskripsi')
                        ->maxLength(255)
                        ->placeholder('Warung / Rumah Makan'),

                    Select::make('sector')
                        ->label('Sektor Usaha')
                        ->options([
                            'Food & Beverage'      => 'Makanan & Minuman',
                            'Retail & Trade'       => 'Perdagangan & Ritel',
                            'Health Services'      => 'Layanan Kesehatan',
                            'IT & Technology'      => 'Teknologi & Informasi',
                            'Construction'         => 'Konstruksi & Bangunan',
                            'Beauty & Personal'    => 'Kecantikan & Perawatan',
                            'Education & Training' => 'Pendidikan & Pelatihan',
                            'Transportation'       => 'Transportasi & Logistik',
                            'Manufacturing'        => 'Manufaktur & Industri',
                            'Accommodation'        => 'Akomodasi & Perhotelan',
                            'Agriculture'          => 'Pertanian & Perkebunan',
                            'Other'                => 'Lainnya',
                        ])
                        ->searchable(),

                    TextInput::make('priority')
                        ->label('Prioritas')
                        ->numeric()
                        ->default(0)
                        ->minValue(-10)
                        ->maxValue(100)
                        ->helperText('Nilai lebih tinggi = dijadwalkan lebih awal. Default: 0'),
                ]),

            Section::make('Status Pipeline')
                ->collapsed()
                ->columns(2)
                ->schema([
                    Select::make('status')
                        ->label('Status')
                        ->options([
                            'pending'  => 'Menunggu',
                            'queued'   => 'Dalam Antrian',
                            'scraping' => 'Sedang Diproses',
                            'done'     => 'Selesai',
                            'failed'   => 'Gagal',
                        ])
                        ->default('pending')
                        ->disabled(),

                    TextInput::make('chroma_chunks')
                        ->label('Chunks ChromaDB')
                        ->numeric()
                        ->disabled(),

                    TextInput::make('scrape_duration_seconds')
                        ->label('Durasi Scraping (detik)')
                        ->numeric()
                        ->disabled(),

                    TextInput::make('last_scraped_at')
                        ->label('Terakhir Diproses')
                        ->disabled(),

                    TextInput::make('scrape_error')
                        ->label('Error (jika ada)')
                        ->disabled()
                        ->columnSpanFull(),
                ]),
        ]);
    }
}
