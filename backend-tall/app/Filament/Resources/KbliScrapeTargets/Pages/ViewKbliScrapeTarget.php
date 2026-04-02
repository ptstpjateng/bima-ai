<?php

namespace App\Filament\Resources\KbliScrapeTargets\Pages;

use App\Filament\Resources\KbliScrapeTargets\KbliScrapeTargetResource;
use App\Services\PipelineService;
use Filament\Actions\Action;
use Filament\Actions\EditAction;
use Filament\Notifications\Notification;
use Filament\Resources\Pages\ViewRecord;

class ViewKbliScrapeTarget extends ViewRecord
{
    protected static string $resource = KbliScrapeTargetResource::class;

    protected function getHeaderActions(): array
    {
        return [
            Action::make('requeue')
                ->label('Re-queue & Jalankan')
                ->icon('heroicon-o-arrow-path')
                ->color('warning')
                ->requiresConfirmation()
                ->modalHeading('Re-queue & Jalankan Pipeline?')
                ->modalDescription('Reset KBLI ini ke status Menunggu dan mulai pipeline scraping.')
                ->action(function () {
                    $this->record->requeue();
                    $result = (new PipelineService())->trigger(1);

                    if ($result['success']) {
                        $msg = ($result['already_running'] ?? false)
                            ? 'Di-requeue. Pipeline sudah berjalan, target akan diproses.'
                            : 'Di-requeue. Pipeline scraping dimulai.';
                        Notification::make()->success()->title('Berhasil')->body($msg)->send();
                    } else {
                        Notification::make()->warning()
                            ->title('Di-requeue, pipeline gagal dimulai')
                            ->body($result['message'])
                            ->send();
                    }

                    $this->refreshFormData(['status', 'scrape_error', 'chroma_chunks']);
                })
                ->visible(fn () => in_array($this->record->status, ['done', 'failed', 'queued', 'scraping'])),

            EditAction::make(),
        ];
    }
}
