<?php

namespace App\Filament\Resources\Kblis\Pages;

use App\Filament\Resources\Kblis\KbliResource;
use Filament\Actions\CreateAction;
use Filament\Resources\Pages\ListRecords;

class ListKblis extends ListRecords
{
    protected static string $resource = KbliResource::class;

    protected function getHeaderActions(): array
    {
        return [
            CreateAction::make(),
        ];
    }
}
