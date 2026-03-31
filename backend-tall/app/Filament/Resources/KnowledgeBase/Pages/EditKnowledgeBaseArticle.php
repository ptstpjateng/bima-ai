<?php

namespace App\Filament\Resources\KnowledgeBase\Pages;

use App\Filament\Resources\KnowledgeBase\KnowledgeBaseResource;
use Filament\Actions\DeleteAction;
use Filament\Resources\Pages\EditRecord;

class EditKnowledgeBaseArticle extends EditRecord
{
    protected static string $resource = KnowledgeBaseResource::class;

    protected function getHeaderActions(): array
    {
        return [
            DeleteAction::make(),
        ];
    }
}
