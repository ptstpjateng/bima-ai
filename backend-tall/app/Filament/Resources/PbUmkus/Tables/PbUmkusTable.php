<?php

namespace App\Filament\Resources\PbUmkus\Tables;

use Filament\Actions\BulkActionGroup;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\EditAction;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Table;

class PbUmkusTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->columns([
                TextColumn::make('nomeklatur')
                    ->label('Nomeklatur')
                    ->searchable()
                    ->sortable()
                    ->weight('bold')
                    ->wrap()
                    ->limit(60),

                TextColumn::make('persyaratan')
                    ->label('Persyaratan')
                    ->limit(50)
                    ->tooltip(fn ($state) => $state)
                    ->wrap()
                    ->toggleable(),

                TextColumn::make('jangka_waktu_penerbitan')
                    ->label('Jangka Waktu')
                    ->badge()
                    ->color('info')
                    ->placeholder('—'),

                TextColumn::make('masa_berlaku')
                    ->label('Masa Berlaku')
                    ->badge()
                    ->color('warning')
                    ->placeholder('—'),

                TextColumn::make('kewenangan')
                    ->label('Kewenangan')
                    ->badge()
                    ->color('gray')
                    ->sortable(),
            ])
            ->actions([
                EditAction::make()->label(''),
            ])
            ->bulkActions([
                BulkActionGroup::make([
                    DeleteBulkAction::make(),
                ]),
            ])
            ->defaultSort('nomeklatur')
            ->striped();
    }
}
