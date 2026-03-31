<?php

namespace App\Filament\Resources\AiInteractions;

use App\Filament\Resources\AiInteractions\Pages\CreateAiInteraction;
use App\Filament\Resources\AiInteractions\Pages\EditAiInteraction;
use App\Filament\Resources\AiInteractions\Pages\ListAiInteractions;
use App\Filament\Resources\AiInteractions\Schemas\AiInteractionForm;
use App\Filament\Resources\AiInteractions\Tables\AiInteractionsTable;
use App\Models\AiInteraction;
use BackedEnum;
use Filament\Resources\Resource;
use Filament\Schemas\Schema;
use Filament\Support\Icons\Heroicon;
use Filament\Tables\Table;

class AiInteractionResource extends Resource
{
    protected static ?string $model = AiInteraction::class;

    protected static string|BackedEnum|null $navigationIcon = Heroicon::OutlinedChatBubbleLeftRight;

    protected static string|\UnitEnum|null $navigationGroup = 'AI';

    protected static ?string $navigationLabel = 'Interactions';

    public static function form(Schema $schema): Schema
    {
        return AiInteractionForm::configure($schema);
    }

    public static function table(Table $table): Table
    {
        return AiInteractionsTable::configure($table);
    }

    public static function getRelations(): array
    {
        return [
            //
        ];
    }

    public static function getPages(): array
    {
        return [
            'index' => ListAiInteractions::route('/'),
            'create' => CreateAiInteraction::route('/create'),
            'edit' => EditAiInteraction::route('/{record}/edit'),
        ];
    }
}
