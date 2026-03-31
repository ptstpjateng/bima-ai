<?php

namespace App\Filament\Resources\AiInteractions\Pages;

use App\Filament\Resources\AiInteractions\AiInteractionResource;
use Filament\Actions\DeleteAction;
use Filament\Resources\Pages\EditRecord;

class EditAiInteraction extends EditRecord
{
    protected static string $resource = AiInteractionResource::class;

    protected function getHeaderActions(): array
    {
        return [
            DeleteAction::make(),
        ];
    }
}
