<?php

namespace App\Filament\Resources\KnowledgeBase\Tables;

use App\Filament\Resources\KnowledgeBase\KnowledgeBaseResource;
use Filament\Actions\BulkActionGroup;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\EditAction;
use Filament\Tables\Columns\BadgeColumn;
use Filament\Tables\Columns\IconColumn;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Filters\SelectFilter;
use Filament\Tables\Table;

class KnowledgeBaseTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->columns([
                TextColumn::make('title')
                    ->label('Judul')
                    ->searchable()
                    ->sortable()
                    ->limit(60),

                TextColumn::make('regulation_type')
                    ->label('Jenis')
                    ->badge()
                    ->formatStateUsing(fn ($state) => match ($state) {
                        'peraturan_pemerintah' => 'PP',
                        'peraturan_daerah'     => 'Perda',
                        'peraturan_menteri'    => 'Permen',
                        'sop_dpmptsp'          => 'SOP',
                        'panduan_oss'          => 'OSS',
                        default                => 'Umum',
                    })
                    ->color(fn ($state) => match ($state) {
                        'sop_dpmptsp'      => 'warning',
                        'peraturan_daerah' => 'info',
                        'panduan_oss'      => 'success',
                        default            => 'gray',
                    }),

                TextColumn::make('region')
                    ->label('Wilayah')
                    ->sortable()
                    ->placeholder('—'),

                IconColumn::make('is_vectorized')
                    ->label('RAG')
                    ->boolean()
                    ->trueIcon('heroicon-o-check-circle')
                    ->falseIcon('heroicon-o-clock')
                    ->trueColor('success')
                    ->falseColor('warning'),

                TextColumn::make('published_at')
                    ->label('Dipublikasikan')
                    ->dateTime('d M Y')
                    ->sortable()
                    ->placeholder('Draft'),

                TextColumn::make('updated_at')
                    ->label('Diperbarui')
                    ->since()
                    ->sortable(),
            ])
            ->filters([
                SelectFilter::make('regulation_type')
                    ->label('Jenis Regulasi')
                    ->options([
                        'peraturan_pemerintah' => 'Peraturan Pemerintah',
                        'peraturan_daerah'     => 'Peraturan Daerah',
                        'peraturan_menteri'    => 'Peraturan Menteri',
                        'sop_dpmptsp'          => 'SOP DPMPTSP',
                        'panduan_oss'          => 'Panduan OSS',
                        'umum'                 => 'Umum',
                    ]),

                SelectFilter::make('is_vectorized')
                    ->label('Status RAG')
                    ->options([
                        '1' => 'Sudah Divektorisasi',
                        '0' => 'Belum Divektorisasi',
                    ]),
            ])
            ->actions([
                EditAction::make(),
            ])
            ->bulkActions([
                BulkActionGroup::make([
                    DeleteBulkAction::make(),
                ]),
            ])
            ->defaultSort('updated_at', 'desc');
    }
}
