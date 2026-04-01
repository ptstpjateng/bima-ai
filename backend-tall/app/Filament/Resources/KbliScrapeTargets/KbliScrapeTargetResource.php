<?php

namespace App\Filament\Resources\KbliScrapeTargets;

use App\Filament\Resources\KbliScrapeTargets\Pages\CreateKbliScrapeTarget;
use App\Filament\Resources\KbliScrapeTargets\Pages\EditKbliScrapeTarget;
use App\Filament\Resources\KbliScrapeTargets\Pages\ListKbliScrapeTargets;
use App\Filament\Resources\KbliScrapeTargets\Schemas\KbliScrapeTargetForm;
use App\Filament\Resources\KbliScrapeTargets\Tables\KbliScrapeTargetsTable;
use App\Models\KbliScrapeTarget;
use BackedEnum;
use Filament\Resources\Resource;
use Filament\Schemas\Schema;
use Filament\Support\Icons\Heroicon;
use Filament\Tables\Table;

class KbliScrapeTargetResource extends Resource
{
    protected static ?string $model = KbliScrapeTarget::class;

    protected static string|BackedEnum|null $navigationIcon = Heroicon::OutlinedCpuChip;

    protected static string|\UnitEnum|null $navigationGroup = 'Knowledge & AI';

    protected static ?string $navigationLabel = 'Pipeline KBLI';

    protected static ?string $modelLabel = 'Target KBLI';

    protected static ?string $pluralModelLabel = 'Pipeline Scraping';

    protected static ?int $navigationSort = 2;

    public static function form(Schema $schema): Schema
    {
        return KbliScrapeTargetForm::configure($schema);
    }

    public static function table(Table $table): Table
    {
        return KbliScrapeTargetsTable::configure($table);
    }

    public static function getRelations(): array
    {
        return [];
    }

    public static function getPages(): array
    {
        return [
            'index'  => ListKbliScrapeTargets::route('/'),
            'create' => CreateKbliScrapeTarget::route('/create'),
            'edit'   => EditKbliScrapeTarget::route('/{record}/edit'),
        ];
    }
}
