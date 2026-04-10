<?php

namespace App\Filament\Resources\PdfParseJobs\Pages;

use App\Filament\Resources\PdfParseJobs\PdfParseJobResource;
use Filament\Resources\Pages\CreateRecord;

class CreatePdfParseJob extends CreateRecord
{
    protected static string $resource = PdfParseJobResource::class;

    protected function mutateFormDataBeforeCreate(array $data): array
    {
        $data['created_by'] = auth()->id();
        $data['status']     = 'pending';
        return $data;
    }

    protected function getRedirectUrl(): string
    {
        return $this->getResource()::getUrl('view', ['record' => $this->record]);
    }
}
