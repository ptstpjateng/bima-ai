<?php

namespace App\Filament\Resources\PermitApplications\Pages;

use App\Filament\Resources\PermitApplications\PermitApplicationResource;
use Filament\Actions\DeleteAction;
use Filament\Resources\Pages\EditRecord;

class EditPermitApplication extends EditRecord
{
    protected static string $resource = PermitApplicationResource::class;

    protected function getHeaderActions(): array
    {
        return [
            DeleteAction::make(),
        ];
    }
}
