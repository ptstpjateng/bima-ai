<?php

namespace App\Filament\Resources\PdfParseJobs\Pages;

use App\Filament\Resources\PdfParseJobs\PdfParseJobResource;
use Filament\Actions\CreateAction;
use Filament\Resources\Pages\ListRecords;

class ListPdfParseJobs extends ListRecords
{
    protected static string $resource = PdfParseJobResource::class;

    protected function getHeaderActions(): array
    {
        return [
            CreateAction::make()
                ->label('Upload PDF Baru'),
        ];
    }
}
