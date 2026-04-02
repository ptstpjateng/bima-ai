<?php

namespace App\Filament\Resources\KnowledgeBase;

use App\Filament\Resources\KnowledgeBase\Pages\CreateKnowledgeBaseArticle;
use App\Filament\Resources\KnowledgeBase\Pages\EditKnowledgeBaseArticle;
use App\Filament\Resources\KnowledgeBase\Pages\ListKnowledgeBaseArticles;
use App\Filament\Resources\KnowledgeBase\Pages\ViewKnowledgeBaseArticle;
use App\Filament\Resources\KnowledgeBase\Schemas\KnowledgeBaseForm;
use App\Filament\Resources\KnowledgeBase\Tables\KnowledgeBaseTable;
use App\Models\KnowledgeBaseArticle;
use BackedEnum;
use Filament\Resources\Resource;
use Filament\Schemas\Schema;
use Filament\Support\Icons\Heroicon;
use Filament\Tables\Table;

class KnowledgeBaseResource extends Resource
{
    protected static ?string $model = KnowledgeBaseArticle::class;

    protected static string|BackedEnum|null $navigationIcon = Heroicon::OutlinedBookOpen;

    protected static string|\UnitEnum|null $navigationGroup = 'Knowledge & AI';

    protected static ?string $navigationLabel = 'Regulasi & Panduan';

    protected static ?string $modelLabel = 'Artikel Regulasi';

    protected static ?string $pluralModelLabel = 'Basis Pengetahuan';

    public static function form(Schema $schema): Schema
    {
        return KnowledgeBaseForm::configure($schema);
    }

    public static function table(Table $table): Table
    {
        return KnowledgeBaseTable::configure($table);
    }

    public static function getRelations(): array
    {
        return [];
    }

    public static function getPages(): array
    {
        return [
            'index'  => ListKnowledgeBaseArticles::route('/'),
            'create' => CreateKnowledgeBaseArticle::route('/create'),
            'view'   => ViewKnowledgeBaseArticle::route('/{record}'),
            'edit'   => EditKnowledgeBaseArticle::route('/{record}/edit'),
        ];
    }
}
