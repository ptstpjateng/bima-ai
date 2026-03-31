<?php

namespace App\Filament\Resources\PermitApplications;

use App\Filament\Resources\PermitApplications\Pages\CreatePermitApplication;
use App\Filament\Resources\PermitApplications\Pages\EditPermitApplication;
use App\Filament\Resources\PermitApplications\Pages\ListPermitApplications;
use App\Filament\Resources\PermitApplications\Schemas\PermitApplicationForm;
use App\Filament\Resources\PermitApplications\Tables\PermitApplicationsTable;
use App\Models\PermitApplication;
use BackedEnum;
use Filament\Resources\Resource;
use Filament\Schemas\Schema;
use Filament\Support\Icons\Heroicon;
use Filament\Tables\Table;

class PermitApplicationResource extends Resource
{
    protected static ?string $model = PermitApplication::class;

    protected static string|BackedEnum|null $navigationIcon = Heroicon::OutlinedDocumentText;

    protected static string|\UnitEnum|null $navigationGroup = 'Permit Management';

    protected static ?string $navigationLabel = 'Applications';

    public static function form(Schema $schema): Schema
    {
        return PermitApplicationForm::configure($schema);
    }

    public static function table(Table $table): Table
    {
        return PermitApplicationsTable::configure($table);
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
            'index' => ListPermitApplications::route('/'),
            'create' => CreatePermitApplication::route('/create'),
            'edit' => EditPermitApplication::route('/{record}/edit'),
        ];
    }
}
