<?php

namespace App\Filament\Resources\PermitApplications\Pages;

use App\Filament\Resources\PermitApplications\PermitApplicationResource;
use Filament\Actions\CreateAction;
use Filament\Resources\Pages\ListRecords;

class ListPermitApplications extends ListRecords
{
    protected static string $resource = PermitApplicationResource::class;

    protected function getHeaderActions(): array
    {
        return [
            CreateAction::make(),
        ];
    }
}
