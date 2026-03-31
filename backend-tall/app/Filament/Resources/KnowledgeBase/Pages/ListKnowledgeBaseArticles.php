<?php

namespace App\Filament\Resources\KnowledgeBase\Pages;

use App\Filament\Resources\KnowledgeBase\KnowledgeBaseResource;
use Filament\Actions\CreateAction;
use Filament\Resources\Pages\ListRecords;

class ListKnowledgeBaseArticles extends ListRecords
{
    protected static string $resource = KnowledgeBaseResource::class;

    protected function getHeaderActions(): array
    {
        return [
            CreateAction::make()->label('Tambah Artikel'),
        ];
    }
}
