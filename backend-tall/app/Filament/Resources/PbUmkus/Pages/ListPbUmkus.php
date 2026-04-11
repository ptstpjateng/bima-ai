<?php

namespace App\Filament\Resources\PbUmkus\Pages;

use App\Filament\Resources\PbUmkus\PbUmkuResource;
use Filament\Actions\CreateAction;
use Filament\Resources\Pages\ListRecords;

class ListPbUmkus extends ListRecords
{
    protected static string $resource = PbUmkuResource::class;

    protected function getHeaderActions(): array
    {
        return [
            CreateAction::make(),
        ];
    }
}
