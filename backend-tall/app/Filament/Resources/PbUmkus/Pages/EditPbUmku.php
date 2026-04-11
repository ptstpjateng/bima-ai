<?php

namespace App\Filament\Resources\PbUmkus\Pages;

use App\Filament\Resources\PbUmkus\PbUmkuResource;
use Filament\Actions\DeleteAction;
use Filament\Resources\Pages\EditRecord;

class EditPbUmku extends EditRecord
{
    protected static string $resource = PbUmkuResource::class;

    protected function getHeaderActions(): array
    {
        return [
            DeleteAction::make(),
        ];
    }
}
