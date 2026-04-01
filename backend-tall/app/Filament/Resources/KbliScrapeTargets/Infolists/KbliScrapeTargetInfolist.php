<?php

namespace App\Filament\Resources\KbliScrapeTargets\Infolists;

use Filament\Infolists\Components\IconEntry;
use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Schema;

class KbliScrapeTargetInfolist
{
    public static function configure(Schema $schema): Schema
    {
        return $schema->components([

            Section::make('Identitas KBLI')
                ->description('Data kode bidang usaha dari sistem OSS RBA')
                ->icon('heroicon-o-identification')
                ->columns(3)
                ->schema([
                    TextEntry::make('kbli_code')
                        ->label('Kode KBLI')
                        ->weight('bold')
                        ->size(TextEntry\TextEntrySize::Large)
                        ->copyable()
                        ->copyMessage('Kode disalin!')
                        ->badge()
                        ->color('primary'),

                    TextEntry::make('kbli_description')
                        ->label('Nama Bidang Usaha')
                        ->columnSpan(2)
                        ->placeholder('—'),

                    TextEntry::make('sector')
                        ->label('Sektor')
                        ->badge()
                        ->color('gray')
                        ->placeholder('—'),

                    TextEntry::make('priority')
                        ->label('Prioritas Scraping')
                        ->badge()
                        ->color(fn ($state) => match(true) {
                            $state >= 8  => 'success',
                            $state >= 4  => 'warning',
                            default      => 'gray',
                        }),

                    TextEntry::make('creator.name')
                        ->label('Ditambahkan Oleh')
                        ->placeholder('Sistem'),
                ]),

            Section::make('Status Pipeline')
                ->description('Hasil pemrosesan data pipeline Playwright')
                ->icon('heroicon-o-cpu-chip')
                ->columns(2)
                ->schema([
                    TextEntry::make('status')
                        ->label('Status')
                        ->badge()
                        ->size(TextEntry\TextEntrySize::Large)
                        ->color(fn (string $state): string => match ($state) {
                            'pending'  => 'gray',
                            'queued'   => 'info',
                            'scraping' => 'warning',
                            'done'     => 'success',
                            'failed'   => 'danger',
                            default    => 'gray',
                        })
                        ->formatStateUsing(fn (string $state): string => match ($state) {
                            'pending'  => 'Menunggu',
                            'queued'   => 'Dalam Antrian',
                            'scraping' => 'Sedang Diproses',
                            'done'     => 'Selesai ✓',
                            'failed'   => 'Gagal ✗',
                            default    => $state,
                        }),

                    TextEntry::make('chroma_chunks')
                        ->label('Chunks di ChromaDB')
                        ->badge()
                        ->color('success')
                        ->formatStateUsing(fn ($state) => $state > 0 ? "{$state} chunks" : 'Belum ada data')
                        ->placeholder('—'),

                    TextEntry::make('last_scraped_at')
                        ->label('Terakhir Diproses')
                        ->dateTime('d M Y, H:i')
                        ->placeholder('Belum pernah diproses'),

                    TextEntry::make('scrape_duration_seconds')
                        ->label('Durasi Scraping')
                        ->formatStateUsing(fn ($state) => $state ? "{$state} detik" : '—')
                        ->placeholder('—'),
                ]),

            Section::make('Detail Error')
                ->description('Informasi error jika scraping gagal')
                ->icon('heroicon-o-exclamation-triangle')
                ->collapsed()
                ->visible(fn ($record) => filled($record->scrape_error))
                ->schema([
                    TextEntry::make('scrape_error')
                        ->label('Pesan Error')
                        ->columnSpanFull()
                        ->color('danger')
                        ->markdown(),
                ]),

        ]);
    }
}
