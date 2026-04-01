<?php

namespace App\Filament\Resources\KbliScrapeTargets\Pages;

use App\Filament\Resources\KbliScrapeTargets\KbliScrapeTargetResource;
use Filament\Actions\DeleteAction;
use Filament\Resources\Pages\EditRecord;

class EditKbliScrapeTarget extends EditRecord
{
    protected static string $resource = KbliScrapeTargetResource::class;

    protected function getHeaderActions(): array
    {
        return [
            DeleteAction::make(),
        ];
    }
}
