<?php

namespace App\Filament\Resources\KbliScrapeTargets\Pages;

use App\Filament\Resources\KbliScrapeTargets\KbliScrapeTargetResource;
use Filament\Actions\CreateAction;
use Filament\Resources\Pages\ListRecords;

class ListKbliScrapeTargets extends ListRecords
{
    protected static string $resource = KbliScrapeTargetResource::class;

    protected function getHeaderActions(): array
    {
        return [
            CreateAction::make(),
        ];
    }
}
