<?php

namespace App\Filament\Resources\PdfParseJobs\Infolists;

use App\Models\PdfParseJob;
use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Schema;
use Filament\Support\Enums\TextSize;

class PdfParseJobInfolist
{
    public static function configure(Schema $schema): Schema
    {
        return $schema->components([

            Section::make('Detail Job')
                ->icon('heroicon-o-document-text')
                ->columns(3)
                ->schema([
                    TextEntry::make('name')
                        ->label('Nama Job')
                        ->weight('bold')
                        ->columnSpan(2),

                    TextEntry::make('created_at')
                        ->label('Dibuat')
                        ->dateTime('d M Y, H:i'),

                    TextEntry::make('kbli_pdf_path')
                        ->label('File KBLI PDF')
                        ->formatStateUsing(fn ($state) => $state ? basename($state) : '—')
                        ->placeholder('Tidak ada'),

                    TextEntry::make('pb_umku_pdf_path')
                        ->label('File PB-UMKU PDF')
                        ->formatStateUsing(fn ($state) => $state ? basename($state) : '—')
                        ->placeholder('Tidak ada'),

                    TextEntry::make('kbli_codes_filter')
                        ->label('Filter KBLI')
                        ->placeholder('Semua KBLI (tidak difilter)'),
                ]),

            Section::make('Status Pipeline')
                ->icon('heroicon-o-cpu-chip')
                ->columns(3)
                ->schema([
                    TextEntry::make('status')
                        ->label('Status')
                        ->badge()
                        ->size(TextSize::Large)
                        ->color(fn (string $state): string => match ($state) {
                            'pending'    => 'gray',
                            'processing' => 'warning',
                            'completed'  => 'success',
                            'failed'     => 'danger',
                            default      => 'gray',
                        })
                        ->formatStateUsing(fn (string $state): string => match ($state) {
                            'pending'    => 'Menunggu',
                            'processing' => 'Sedang Diproses',
                            'completed'  => 'Selesai ✓',
                            'failed'     => 'Gagal ✗',
                            default      => $state,
                        }),

                    TextEntry::make('progress_pct')
                        ->label('Progres')
                        ->formatStateUsing(fn ($state) => "{$state}%")
                        ->badge()
                        ->color(fn ($state) => match(true) {
                            $state >= 100 => 'success',
                            $state >= 50  => 'info',
                            $state > 0    => 'warning',
                            default       => 'gray',
                        }),

                    TextEntry::make('chunks_created')
                        ->label('Chunks ChromaDB')
                        ->formatStateUsing(fn ($state) => $state > 0 ? "{$state} chunks" : 'Belum ada')
                        ->badge()
                        ->color('success'),

                    TextEntry::make('kbli_processed')
                        ->label('KBLI Diproses')
                        ->formatStateUsing(fn ($state) => "{$state} kode")
                        ->badge()
                        ->color('primary'),

                    TextEntry::make('started_at')
                        ->label('Mulai Diproses')
                        ->dateTime('d M Y, H:i:s')
                        ->placeholder('Belum dimulai'),

                    TextEntry::make('completed_at')
                        ->label('Selesai')
                        ->dateTime('d M Y, H:i:s')
                        ->placeholder('—'),
                ]),

            Section::make('Error Detail')
                ->icon('heroicon-o-exclamation-triangle')
                ->collapsed()
                ->visible(fn (PdfParseJob $record) => filled($record->error_message))
                ->schema([
                    TextEntry::make('error_message')
                        ->label('Pesan Error')
                        ->columnSpanFull()
                        ->color('danger')
                        ->markdown(),
                ]),

            Section::make('Hasil Merge JSON')
                ->description('Output gabungan dari Agent 2 — disimpan untuk audit admin.')
                ->icon('heroicon-o-code-bracket')
                ->collapsed()
                ->visible(fn (PdfParseJob $record) => filled($record->result_json))
                ->schema([
                    TextEntry::make('result_json')
                        ->label('')
                        ->columnSpanFull()
                        ->formatStateUsing(function ($state) {
                            if (! $state) {
                                return '_Tidak ada data._';
                            }
                            $decoded = json_decode($state, true);
                            if ($decoded === null) {
                                return $state;
                            }
                            $count = is_array($decoded) ? count($decoded) : '?';
                            $preview = json_encode($decoded, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);
                            return "**{$count} KBLI dimerge**\n\n```json\n" . substr($preview, 0, 5000) . "\n```";
                        })
                        ->markdown()
                        ->extraAttributes(['class' => 'max-h-[70vh] overflow-y-auto text-sm font-mono']),
                ]),

        ]);
    }
}
