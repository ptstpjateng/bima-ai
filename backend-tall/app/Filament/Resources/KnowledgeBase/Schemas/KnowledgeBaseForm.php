<?php

namespace App\Filament\Resources\KnowledgeBase\Schemas;

use Filament\Forms\Components\DateTimePicker;
use Filament\Forms\Components\FileUpload;
use Filament\Forms\Components\Hidden;
use Filament\Forms\Components\Section;
use Filament\Forms\Components\Select;
use Filament\Forms\Components\TagsInput;
use Filament\Forms\Components\Textarea;
use Filament\Forms\Components\TextInput;
use Filament\Forms\Components\Toggle;
use Filament\Schemas\Schema;

class KnowledgeBaseForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema->components([

            Section::make('Informasi Artikel')
                ->columns(2)
                ->schema([
                    TextInput::make('title')
                        ->label('Judul Regulasi / Panduan')
                        ->required()
                        ->maxLength(255)
                        ->columnSpanFull(),

                    Select::make('source_type')
                        ->label('Sumber')
                        ->options([
                            'manual' => 'Tulis Manual',
                            'pdf'    => 'Upload PDF',
                            'url'    => 'URL Referensi',
                        ])
                        ->default('manual')
                        ->live()
                        ->required(),

                    Select::make('regulation_type')
                        ->label('Jenis Regulasi')
                        ->options([
                            'peraturan_pemerintah' => 'Peraturan Pemerintah',
                            'peraturan_daerah'     => 'Peraturan Daerah',
                            'peraturan_menteri'    => 'Peraturan Menteri',
                            'sop_dpmptsp'          => 'SOP DPMPTSP',
                            'panduan_oss'          => 'Panduan OSS',
                            'umum'                 => 'Umum',
                        ])
                        ->default('umum')
                        ->required(),

                    TextInput::make('region')
                        ->label('Wilayah Berlaku')
                        ->placeholder('e.g. Jawa Tengah, Semarang, Nasional')
                        ->maxLength(100),

                    TextInput::make('source_url')
                        ->label('URL Referensi')
                        ->url()
                        ->maxLength(500)
                        ->visible(fn ($get) => $get('source_type') === 'url'),

                    FileUpload::make('pdf_path')
                        ->label('Upload PDF')
                        ->disk('public')
                        ->directory('knowledge-base/pdfs')
                        ->acceptedFileTypes(['application/pdf'])
                        ->maxSize(20480)
                        ->columnSpanFull()
                        ->visible(fn ($get) => $get('source_type') === 'pdf'),
                ]),

            Section::make('Konten Regulasi')
                ->schema([
                    Textarea::make('content')
                        ->label('Isi Regulasi / Panduan')
                        ->helperText('Teks lengkap yang akan diindeks ke dalam sistem RAG (ChromaDB). Semakin detail, semakin akurat jawaban AI.')
                        ->required()
                        ->rows(15)
                        ->columnSpanFull(),
                ]),

            Section::make('Klasifikasi & Penandaan')
                ->columns(2)
                ->schema([
                    TagsInput::make('kbli_codes')
                        ->label('Kode KBLI Terkait')
                        ->placeholder('Tambah kode KBLI, tekan Enter')
                        ->helperText('Kosongkan jika berlaku untuk semua bidang usaha'),

                    TagsInput::make('tags')
                        ->label('Tag / Label')
                        ->placeholder('e.g. NIB, SIUP, K3, lingkungan'),

                    Toggle::make('is_published_now')
                        ->label('Publikasikan Sekarang')
                        ->helperText('Artikel yang dipublikasikan akan tersedia untuk RAG')
                        ->dehydrated(false)
                        ->live()
                        ->afterStateUpdated(
                            fn (bool $state, callable $set) => $set('published_at', $state ? now()->toDateTimeString() : null)
                        )
                        ->formatStateUsing(fn ($state, $record) => $record?->published_at !== null),

                    Hidden::make('published_at'),
                ]),

            Section::make('Status Vektorisasi')
                ->collapsed()
                ->schema([
                    Toggle::make('is_vectorized')
                        ->label('Sudah Divektorisasi')
                        ->disabled(),

                    DateTimePicker::make('vectorized_at')
                        ->label('Waktu Vektorisasi')
                        ->disabled(),

                    TextInput::make('chroma_doc_id')
                        ->label('ChromaDB Document ID')
                        ->disabled(),

                    Textarea::make('vectorize_error')
                        ->label('Error Vektorisasi (jika ada)')
                        ->disabled()
                        ->rows(3),
                ]),
        ]);
    }
}
