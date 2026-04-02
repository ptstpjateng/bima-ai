<?php

namespace App\Filament\Resources\KnowledgeBase\Infolists;

use Filament\Infolists\Components\IconEntry;
use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Schema;

class KnowledgeBaseInfolist
{
    public static function configure(Schema $schema): Schema
    {
        return $schema
            ->columns(3)
            ->components([
                Section::make('Metadata Artikel')
                    ->columnSpan(1)
                    ->columns(1)
                    ->components([
                        TextEntry::make('title')
                            ->label('Judul')
                            ->weight('bold')
                            ->size('lg'),

                        TextEntry::make('regulation_type')
                            ->label('Jenis Regulasi')
                            ->badge()
                            ->formatStateUsing(fn ($state) => match ($state) {
                                'peraturan_pemerintah' => 'Peraturan Pemerintah',
                                'peraturan_daerah'     => 'Peraturan Daerah',
                                'peraturan_menteri'    => 'Peraturan Menteri',
                                'sop_dpmptsp'          => 'SOP DPMPTSP',
                                'panduan_oss'          => 'Panduan OSS',
                                default                => 'Umum',
                            })
                            ->color(fn ($state) => match ($state) {
                                'sop_dpmptsp'          => 'warning',
                                'peraturan_daerah'     => 'info',
                                'panduan_oss'          => 'success',
                                default                => 'gray',
                            }),

                        TextEntry::make('region')
                            ->label('Wilayah')
                            ->placeholder('—'),

                        TextEntry::make('source_url')
                            ->label('URL Sumber')
                            ->url(fn ($state) => $state)
                            ->openUrlInNewTab()
                            ->placeholder('—')
                            ->limit(50),

                        TextEntry::make('kbli_codes')
                            ->label('Kode KBLI')
                            ->badge()
                            ->separator(',')
                            ->placeholder('—'),

                        TextEntry::make('tags')
                            ->label('Tags')
                            ->badge()
                            ->separator(',')
                            ->color('gray')
                            ->placeholder('—'),
                    ]),

                Section::make('Status RAG & Vektorisasi')
                    ->columnSpan(1)
                    ->columns(1)
                    ->components([
                        IconEntry::make('is_vectorized')
                            ->label('Status Vektorisasi')
                            ->boolean()
                            ->trueIcon('heroicon-o-check-circle')
                            ->falseIcon('heroicon-o-clock')
                            ->trueColor('success')
                            ->falseColor('warning'),

                        TextEntry::make('chroma_doc_id')
                            ->label('ChromaDB Doc ID')
                            ->copyable()
                            ->fontFamily('mono')
                            ->placeholder('Belum divektorisasi'),

                        TextEntry::make('vectorized_at')
                            ->label('Divektorisasi Pada')
                            ->dateTime('d M Y, H:i')
                            ->placeholder('—'),

                        TextEntry::make('vectorize_error')
                            ->label('Error Vektorisasi')
                            ->color('danger')
                            ->placeholder('—'),

                        TextEntry::make('published_at')
                            ->label('Dipublikasikan')
                            ->dateTime('d M Y, H:i')
                            ->placeholder('Draft — belum dipublikasikan'),

                        TextEntry::make('author.name')
                            ->label('Dibuat Oleh')
                            ->placeholder('—'),

                        TextEntry::make('updated_at')
                            ->label('Terakhir Diperbarui')
                            ->since(),
                    ]),

                Section::make('Konten Terskraping (RAG Debugger)')
                    ->description('Teks mentah yang diindeks ke ChromaDB — gunakan ini untuk memverifikasi kualitas data RAG')
                    ->columnSpan(3)
                    ->components([
                        TextEntry::make('content')
                            ->label('')
                            ->prose()
                            ->columnSpanFull()
                            ->placeholder('Tidak ada konten — artikel ini belum memiliki teks yang dapat divektorisasi.')
                            ->extraAttributes([
                                'class' => 'max-h-[70vh] overflow-y-auto font-mono text-xs leading-relaxed bg-slate-50 dark:bg-slate-900 p-4 rounded-lg',
                            ]),
                    ]),
            ]);
    }
}
