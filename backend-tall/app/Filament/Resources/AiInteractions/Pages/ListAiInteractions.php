<?php

namespace App\Filament\Resources\AiInteractions\Pages;

use App\Filament\Resources\AiInteractions\AiInteractionResource;
use Filament\Actions\CreateAction;
use Filament\Resources\Pages\ListRecords;

class ListAiInteractions extends ListRecords
{
    protected static string $resource = AiInteractionResource::class;

    protected function getHeaderActions(): array
    {
        return [
            CreateAction::make(),
        ];
    }
}
