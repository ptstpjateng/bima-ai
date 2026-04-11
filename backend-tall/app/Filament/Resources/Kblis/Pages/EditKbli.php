<?php

namespace App\Filament\Resources\Kblis\Pages;

use App\Filament\Resources\Kblis\KbliResource;
use Filament\Actions\DeleteAction;
use Filament\Resources\Pages\EditRecord;

class EditKbli extends EditRecord
{
    protected static string $resource = KbliResource::class;

    protected function getHeaderActions(): array
    {
        return [
            DeleteAction::make(),
        ];
    }
}
