<?php

namespace App\Filament\Resources\KbliScrapeTargets\Pages;

use App\Filament\Resources\KbliScrapeTargets\KbliScrapeTargetResource;
use Filament\Resources\Pages\CreateRecord;

class CreateKbliScrapeTarget extends CreateRecord
{
    protected static string $resource = KbliScrapeTargetResource::class;

    protected function mutateFormDataBeforeCreate(array $data): array
    {
        $data['created_by'] = auth()->id();
        return $data;
    }
}
