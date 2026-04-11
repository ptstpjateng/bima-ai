<?php

namespace App\Filament\Resources\PbUmkus;

use App\Filament\Resources\PbUmkus\Pages\CreatePbUmku;
use App\Filament\Resources\PbUmkus\Pages\EditPbUmku;
use App\Filament\Resources\PbUmkus\Pages\ListPbUmkus;
use App\Filament\Resources\PbUmkus\Schemas\PbUmkuForm;
use App\Filament\Resources\PbUmkus\Tables\PbUmkusTable;
use App\Models\PbUmku;
use BackedEnum;
use Filament\Resources\Resource;
use Filament\Schemas\Schema;
use Filament\Support\Icons\Heroicon;
use Filament\Tables\Table;

class PbUmkuResource extends Resource
{
    protected static ?string $model = PbUmku::class;

    protected static string|BackedEnum|null $navigationIcon = Heroicon::OutlinedClipboardDocumentList;

    protected static string|\UnitEnum|null $navigationGroup = 'Data Hub';

    protected static ?string $navigationLabel = 'PB UMKU';

    protected static ?string $modelLabel = 'PB UMKU';

    protected static ?string $pluralModelLabel = 'Data PB UMKU';

    protected static ?int $navigationSort = 2;

    public static function form(Schema $schema): Schema
    {
        return PbUmkuForm::configure($schema);
    }

    public static function table(Table $table): Table
    {
        return PbUmkusTable::configure($table);
    }

    public static function getPages(): array
    {
        return [
            'index'  => ListPbUmkus::route('/'),
            'create' => CreatePbUmku::route('/create'),
            'edit'   => EditPbUmku::route('/{record}/edit'),
        ];
    }
}
