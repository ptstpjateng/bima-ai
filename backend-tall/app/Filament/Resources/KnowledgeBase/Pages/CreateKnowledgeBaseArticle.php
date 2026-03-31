<?php

namespace App\Filament\Resources\KnowledgeBase\Pages;

use App\Filament\Resources\KnowledgeBase\KnowledgeBaseResource;
use Filament\Resources\Pages\CreateRecord;

class CreateKnowledgeBaseArticle extends CreateRecord
{
    protected static string $resource = KnowledgeBaseResource::class;

    protected function mutateFormDataBeforeCreate(array $data): array
    {
        $data['created_by'] = auth()->id();
        return $data;
    }
}
