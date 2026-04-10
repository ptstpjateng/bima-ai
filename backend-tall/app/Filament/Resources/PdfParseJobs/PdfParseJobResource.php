<?php

namespace App\Filament\Resources\PdfParseJobs;

use App\Filament\Resources\PdfParseJobs\Infolists\PdfParseJobInfolist;
use App\Filament\Resources\PdfParseJobs\Pages\CreatePdfParseJob;
use App\Filament\Resources\PdfParseJobs\Pages\ListPdfParseJobs;
use App\Filament\Resources\PdfParseJobs\Pages\ViewPdfParseJob;
use App\Filament\Resources\PdfParseJobs\Schemas\PdfParseJobForm;
use App\Filament\Resources\PdfParseJobs\Tables\PdfParseJobTable;
use App\Models\PdfParseJob;
use BackedEnum;
use Filament\Resources\Resource;
use Filament\Schemas\Schema;
use Filament\Support\Icons\Heroicon;
use Filament\Tables\Table;

class PdfParseJobResource extends Resource
{
    protected static ?string $model = PdfParseJob::class;

    protected static string|BackedEnum|null $navigationIcon = Heroicon::OutlinedDocumentArrowUp;

    protected static string|\UnitEnum|null $navigationGroup = 'Knowledge & AI';

    protected static ?string $navigationLabel = 'PDF Parser';

    protected static ?string $modelLabel = 'PDF Parse Job';

    protected static ?string $pluralModelLabel = 'Multi-Agent PDF Parser';

    protected static ?int $navigationSort = 3;

    public static function form(Schema $schema): Schema
    {
        return PdfParseJobForm::configure($schema);
    }

    public static function infolist(Schema $schema): Schema
    {
        return PdfParseJobInfolist::configure($schema);
    }

    public static function table(Table $table): Table
    {
        return PdfParseJobTable::configure($table);
    }

    public static function getRelations(): array
    {
        return [];
    }

    public static function getPages(): array
    {
        return [
            'index'  => ListPdfParseJobs::route('/'),
            'create' => CreatePdfParseJob::route('/create'),
            'view'   => ViewPdfParseJob::route('/{record}'),
        ];
    }
}
