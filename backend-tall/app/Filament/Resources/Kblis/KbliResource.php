<?php

namespace App\Filament\Resources\Kblis;

use App\Filament\Resources\Kblis\Pages\CreateKbli;
use App\Filament\Resources\Kblis\Pages\EditKbli;
use App\Filament\Resources\Kblis\Pages\ListKblis;
use App\Filament\Resources\Kblis\Schemas\KbliForm;
use App\Filament\Resources\Kblis\Tables\KblisTable;
use App\Models\Kbli;
use BackedEnum;
use Filament\Resources\Resource;
use Filament\Schemas\Schema;
use Filament\Support\Icons\Heroicon;
use Filament\Tables\Table;

class KbliResource extends Resource
{
    protected static ?string $model = Kbli::class;

    protected static string|BackedEnum|null $navigationIcon = Heroicon::OutlinedBookOpen;

    protected static string|\UnitEnum|null $navigationGroup = 'Data Hub';

    protected static ?string $navigationLabel = 'KBLI';

    protected static ?string $modelLabel = 'KBLI';

    protected static ?string $pluralModelLabel = 'Data KBLI';

    protected static ?int $navigationSort = 1;

    public static function form(Schema $schema): Schema
    {
        return KbliForm::configure($schema);
    }

    public static function table(Table $table): Table
    {
        return KblisTable::configure($table);
    }

    public static function getPages(): array
    {
        return [
            'index'  => ListKblis::route('/'),
            'create' => CreateKbli::route('/create'),
            'edit'   => EditKbli::route('/{record}/edit'),
        ];
    }
}
