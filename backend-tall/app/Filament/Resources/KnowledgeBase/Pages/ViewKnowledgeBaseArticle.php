<?php

namespace App\Filament\Resources\KnowledgeBase\Pages;

use App\Filament\Resources\KnowledgeBase\Infolists\KnowledgeBaseInfolist;
use App\Filament\Resources\KnowledgeBase\KnowledgeBaseResource;
use Filament\Actions\EditAction;
use Filament\Resources\Pages\ViewRecord;
use Filament\Schemas\Schema;

class ViewKnowledgeBaseArticle extends ViewRecord
{
    protected static string $resource = KnowledgeBaseResource::class;

    public function infolist(Schema $schema): Schema
    {
        return KnowledgeBaseInfolist::configure($schema);
    }

    protected function getHeaderActions(): array
    {
        return [
            EditAction::make(),
        ];
    }
}
