<?php

namespace App\Filament\Resources\Kblis\Tables;

use Filament\Actions\BulkActionGroup;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\EditAction;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Filters\SelectFilter;
use Filament\Tables\Table;

class KblisTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->columns([
                TextColumn::make('kode_kbli')
                    ->label('Kode KBLI')
                    ->searchable()
                    ->sortable()
                    ->weight('bold')
                    ->copyable()
                    ->color('primary'),

                TextColumn::make('judul_kbli')
                    ->label('Judul')
                    ->searchable()
                    ->limit(50)
                    ->tooltip(fn ($state) => $state)
                    ->wrap(),

                TextColumn::make('skala_usaha')
                    ->label('Skala')
                    ->badge()
                    ->color(fn (?string $state): string => match ($state) {
                        'Mikro' => 'info',
                        'Kecil' => 'success',
                        'Menengah' => 'warning',
                        'Besar' => 'danger',
                        'Seluruh' => 'gray',
                        default => 'gray',
                    })
                    ->sortable(),

                TextColumn::make('tingkat_risiko')
                    ->label('Risiko')
                    ->badge()
                    ->color(fn (?string $state): string => match (true) {
                        str_contains($state ?? '', 'Rendah') => 'success',
                        str_contains($state ?? '', 'Menengah') => 'warning',
                        str_contains($state ?? '', 'Tinggi') => 'danger',
                        default => 'gray',
                    })
                    ->sortable(),

                TextColumn::make('perizinan_berusaha')
                    ->label('Perizinan')
                    ->limit(35)
                    ->tooltip(fn ($state) => $state)
                    ->toggleable(),

                TextColumn::make('sektor')
                    ->label('Sektor')
                    ->badge()
                    ->color('info')
                    ->sortable()
                    ->searchable(),

                TextColumn::make('kewenangan')
                    ->label('Kewenangan')
                    ->badge()
                    ->color('gray')
                    ->sortable(),

                TextColumn::make('pb_umku_names')
                    ->label('PB UMKU')
                    ->limit(40)
                    ->tooltip(fn ($state) => $state)
                    ->placeholder('—')
                    ->toggleable(isToggledHiddenByDefault: true),
            ])
            ->filters([
                SelectFilter::make('tingkat_risiko')
                    ->label('Tingkat Risiko')
                    ->options([
                        'Rendah' => 'Rendah',
                        'Menengah Rendah' => 'Menengah Rendah',
                        'Menengah Tinggi' => 'Menengah Tinggi',
                        'Tinggi' => 'Tinggi',
                    ]),

                SelectFilter::make('skala_usaha')
                    ->label('Skala Usaha')
                    ->options([
                        'Mikro' => 'Mikro',
                        'Kecil' => 'Kecil',
                        'Menengah' => 'Menengah',
                        'Besar' => 'Besar',
                        'Seluruh' => 'Seluruh',
                    ]),

                SelectFilter::make('sektor')
                    ->label('Sektor')
                    ->options(fn () => \App\Models\Kbli::query()
                        ->whereNotNull('sektor')
                        ->where('sektor', '!=', '')
                        ->distinct()
                        ->pluck('sektor', 'sektor')
                        ->toArray()
                    ),

                SelectFilter::make('kewenangan')
                    ->label('Kewenangan')
                    ->options(fn () => \App\Models\Kbli::query()
                        ->whereNotNull('kewenangan')
                        ->distinct()
                        ->pluck('kewenangan', 'kewenangan')
                        ->toArray()
                    ),
            ])
            ->actions([
                EditAction::make()->label(''),
            ])
            ->bulkActions([
                BulkActionGroup::make([
                    DeleteBulkAction::make(),
                ]),
            ])
            ->defaultSort('kode_kbli')
            ->striped();
    }
}
