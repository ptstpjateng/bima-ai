<?php

namespace App\Filament\Resources\PdfParseJobs\Pages;

use App\Filament\Resources\PdfParseJobs\PdfParseJobResource;
use App\Models\PdfParseJob;
use App\Services\PdfPipelineService;
use Filament\Actions\Action;
use Filament\Notifications\Notification;
use Filament\Resources\Pages\ViewRecord;

class ViewPdfParseJob extends ViewRecord
{
    protected static string $resource = PdfParseJobResource::class;

    // Auto-refresh the view every 15 s while the pipeline is running
    protected ?string $pollingInterval = '15s';

    protected function getHeaderActions(): array
    {
        /** @var PdfParseJob $record */
        $record = $this->record;

        return [
            Action::make('run')
                ->label('Jalankan Pipeline')
                ->icon('heroicon-o-play')
                ->color('success')
                ->requiresConfirmation()
                ->modalHeading('Jalankan Multi-Agent PDF Pipeline?')
                ->modalDescription(
                    "Pipeline 3-agent akan berjalan di background:\n" .
                    "  Agent 1 → Ekstrak PDF ke Markdown\n" .
                    "  Agent 2 → Struktur & Match KBLI↔PB-UMKU\n" .
                    "  Agent 3 → Vektorisasi ke ChromaDB\n\n" .
                    "Halaman ini akan otomatis refresh setiap 15 detik."
                )
                ->action(function () use ($record) {
                    if ($record->isRunning()) {
                        Notification::make()->warning()->title('Pipeline sudah berjalan.')->send();
                        return;
                    }
                    $result = (new PdfPipelineService())->triggerPdfJob($record->id);
                    if ($result['success']) {
                        Notification::make()->success()
                            ->title('Pipeline dimulai!')
                            ->body('Halaman akan otomatis diperbarui setiap 15 detik.')
                            ->send();
                    } else {
                        Notification::make()->danger()
                            ->title('Gagal menghubungi pipeline server')
                            ->body($result['message'])
                            ->send();
                    }
                })
                ->visible(fn () => in_array($record->status, ['pending', 'failed'])),

            Action::make('reset')
                ->label('Reset')
                ->icon('heroicon-o-arrow-path')
                ->color('gray')
                ->requiresConfirmation()
                ->action(function () use ($record) {
                    $record->reset();
                    Notification::make()->success()->title('Job direset ke status Menunggu.')->send();
                    $this->refreshFormData(['status', 'progress_pct', 'error_message']);
                })
                ->visible(fn () => in_array($record->status, ['completed', 'failed', 'processing'])),
        ];
    }
}
