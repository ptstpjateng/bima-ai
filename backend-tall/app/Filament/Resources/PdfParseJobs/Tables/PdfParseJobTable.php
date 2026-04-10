<?php

namespace App\Filament\Resources\PdfParseJobs\Tables;

use App\Filament\Resources\PdfParseJobs\PdfParseJobResource;
use App\Models\PdfParseJob;
use App\Services\PdfPipelineService;
use Filament\Actions\Action;
use Filament\Actions\BulkActionGroup;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\ViewAction;
use Filament\Notifications\Notification;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Filters\SelectFilter;
use Filament\Tables\Table;

class PdfParseJobTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->columns([
                TextColumn::make('id')
                    ->label('#')
                    ->sortable()
                    ->width('60px'),

                TextColumn::make('name')
                    ->label('Nama Job')
                    ->searchable()
                    ->weight('bold')
                    ->limit(45),

                TextColumn::make('status')
                    ->label('Status')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'pending'    => 'gray',
                        'processing' => 'warning',
                        'completed'  => 'success',
                        'failed'     => 'danger',
                        default      => 'gray',
                    })
                    ->formatStateUsing(fn (string $state): string => match ($state) {
                        'pending'    => 'Menunggu',
                        'processing' => 'Diproses',
                        'completed'  => 'Selesai ✓',
                        'failed'     => 'Gagal ✗',
                        default      => $state,
                    }),

                TextColumn::make('progress_pct')
                    ->label('Progres')
                    ->formatStateUsing(fn ($state) => "{$state}%")
                    ->badge()
                    ->color(fn ($state) => match(true) {
                        $state >= 100 => 'success',
                        $state >= 50  => 'info',
                        $state > 0    => 'warning',
                        default       => 'gray',
                    }),

                TextColumn::make('kbli_processed')
                    ->label('KBLI')
                    ->formatStateUsing(fn ($state) => $state > 0 ? "{$state} kode" : '—')
                    ->badge()
                    ->color('primary'),

                TextColumn::make('chunks_created')
                    ->label('Chunks')
                    ->formatStateUsing(fn ($state) => $state > 0 ? "{$state} chunks" : '—')
                    ->badge()
                    ->color('success'),

                TextColumn::make('creator.name')
                    ->label('Dibuat Oleh')
                    ->placeholder('Sistem')
                    ->toggleable(isToggledHiddenByDefault: true),

                TextColumn::make('created_at')
                    ->label('Dibuat')
                    ->dateTime('d M, H:i')
                    ->sortable(),
            ])
            ->filters([
                SelectFilter::make('status')
                    ->label('Status')
                    ->options([
                        'pending'    => 'Menunggu',
                        'processing' => 'Diproses',
                        'completed'  => 'Selesai',
                        'failed'     => 'Gagal',
                    ]),
            ])
            ->recordActions([
                ViewAction::make()->label(''),

                Action::make('run')
                    ->label('')
                    ->tooltip('Jalankan Pipeline PDF')
                    ->icon('heroicon-o-play')
                    ->color('success')
                    ->requiresConfirmation()
                    ->modalHeading('Jalankan Multi-Agent PDF Pipeline?')
                    ->modalDescription(fn (PdfParseJob $record) =>
                        "Pipeline akan memproses job \"{$record->name}\" menggunakan 3 AI agent:\n" .
                        "1. Extractor → PDF ke Markdown\n" .
                        "2. Structurer → Parsing & Matching KBLI + PB-UMKU\n" .
                        "3. Ingestor → ChromaDB vectorization\n\n" .
                        "Proses berjalan di background. Refresh halaman untuk melihat progres."
                    )
                    ->action(function (PdfParseJob $record) {
                        if ($record->isRunning()) {
                            Notification::make()
                                ->warning()
                                ->title('Pipeline sedang berjalan')
                                ->body("Job #{$record->id} sudah dalam status 'Diproses'.")
                                ->send();
                            return;
                        }

                        $result = (new PdfPipelineService())->triggerPdfJob($record->id);

                        if ($result['success']) {
                            Notification::make()
                                ->success()
                                ->title('Pipeline dimulai!')
                                ->body("Job \"{$record->name}\" sedang diproses. Refresh untuk melihat progres.")
                                ->send();
                        } else {
                            Notification::make()
                                ->danger()
                                ->title('Gagal memulai pipeline')
                                ->body($result['message'])
                                ->send();
                        }
                    })
                    ->visible(fn (PdfParseJob $record) => in_array($record->status, ['pending', 'failed'])),

                Action::make('reset')
                    ->label('')
                    ->tooltip('Reset ke Menunggu')
                    ->icon('heroicon-o-arrow-path')
                    ->color('gray')
                    ->requiresConfirmation()
                    ->action(function (PdfParseJob $record) {
                        $record->reset();
                        Notification::make()->success()->title('Job direset ke status Menunggu.')->send();
                    })
                    ->visible(fn (PdfParseJob $record) => in_array($record->status, ['completed', 'failed', 'processing'])),
            ])
            ->toolbarActions([
                BulkActionGroup::make([
                    DeleteBulkAction::make(),
                ]),
            ])
            ->defaultSort('created_at', 'desc')
            ->striped()
            ->poll('20s');
    }
}
