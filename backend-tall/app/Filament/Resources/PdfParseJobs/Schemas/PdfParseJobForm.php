<?php

namespace App\Filament\Resources\PdfParseJobs\Schemas;

use Filament\Forms\Components\FileUpload;
use Filament\Forms\Components\TextInput;
use Filament\Forms\Components\Textarea;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Schema;

class PdfParseJobForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema->components([

            Section::make('Identifikasi Job')
                ->description('Beri nama yang deskriptif agar mudah diidentifikasi di antrian.')
                ->icon('heroicon-o-document-text')
                ->schema([
                    TextInput::make('name')
                        ->label('Nama Job')
                        ->placeholder('Contoh: KBLI Makanan & Minuman – April 2026')
                        ->required()
                        ->maxLength(255)
                        ->columnSpanFull(),

                    Textarea::make('kbli_codes_filter')
                        ->label('Filter Kode KBLI (Opsional)')
                        ->placeholder("56102, 56103, 56209\n(Kosongkan untuk memproses semua KBLI yang ada di PDF)")
                        ->helperText('Satu kode per baris atau dipisahkan koma. Jika diisi, hanya kode-kode ini yang akan diproses dari PDF.')
                        ->rows(3)
                        ->columnSpanFull(),
                ]),

            Section::make('Upload Dokumen PDF')
                ->description('Upload dua jenis PDF yang akan diproses oleh pipeline multi-agent.')
                ->icon('heroicon-o-arrow-up-tray')
                ->columns(2)
                ->schema([
                    FileUpload::make('kbli_pdf_path')
                        ->label('KBLI Detail PDF')
                        ->helperText('PDF dari OSS yang berisi detail KBLI: uraian, ruang lingkup, nama PB-UMKU yang dibutuhkan.')
                        ->disk('local')
                        ->directory('pdf_datasets/kbli')
                        ->visibility('private')
                        ->acceptedFileTypes(['application/pdf'])
                        ->maxSize(307200),  // 300 MB

                    FileUpload::make('pb_umku_pdf_path')
                        ->label('PB-UMKU Detail PDF')
                        ->helperText('PDF tabel besar dari OSS berisi detail PB-UMKU: persyaratan, kewajiban, sanksi, jangka waktu, dll.')
                        ->disk('local')
                        ->directory('pdf_datasets/pb_umku')
                        ->visibility('private')
                        ->acceptedFileTypes(['application/pdf'])
                        ->maxSize(307200),  // 300 MB
                ]),

        ]);
    }
}
