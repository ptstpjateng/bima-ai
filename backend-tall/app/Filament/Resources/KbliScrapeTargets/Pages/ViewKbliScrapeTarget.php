<?php

namespace App\Filament\Resources\KbliScrapeTargets\Pages;

use App\Filament\Resources\KbliScrapeTargets\KbliScrapeTargetResource;
use App\Models\KbliScrapeTarget;
use Filament\Actions\Action;
use Filament\Actions\EditAction;
use Filament\Resources\Pages\ViewRecord;

class ViewKbliScrapeTarget extends ViewRecord
{
    protected static string $resource = KbliScrapeTargetResource::class;

    protected function getHeaderActions(): array
    {
        return [
            Action::make('requeue')
                ->label('Re-queue')
                ->icon('heroicon-o-arrow-path')
                ->color('warning')
                ->requiresConfirmation()
                ->action(fn () => $this->record->requeue())
                ->after(fn () => $this->refreshFormData([]))
                ->visible(fn () => in_array($this->record->status, ['done', 'failed', 'queued'])),

            EditAction::make(),
        ];
    }
}
