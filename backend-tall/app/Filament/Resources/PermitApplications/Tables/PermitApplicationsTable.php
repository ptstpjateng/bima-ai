<?php

namespace App\Filament\Resources\PermitApplications\Tables;

use Filament\Actions\BulkActionGroup;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\EditAction;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Filters\SelectFilter;
use Filament\Tables\Table;

class PermitApplicationsTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->columns([
                TextColumn::make('application_number')
                    ->searchable()
                    ->sortable(),

                TextColumn::make('user.name')
                    ->label('Applicant')
                    ->searchable()
                    ->sortable(),

                TextColumn::make('kbli_code')
                    ->searchable(),

                TextColumn::make('business_scale')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'mikro' => 'gray',
                        'kecil' => 'info',
                        'menengah' => 'warning',
                        'besar' => 'danger',
                        default => 'gray',
                    }),

                TextColumn::make('risk_level')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'rendah' => 'success',
                        'menengah_rendah' => 'info',
                        'menengah_tinggi' => 'warning',
                        'tinggi' => 'danger',
                        default => 'gray',
                    }),

                TextColumn::make('status')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'draft' => 'gray',
                        'submitted' => 'info',
                        'under_review' => 'warning',
                        'additional_docs_required' => 'warning',
                        'approved' => 'success',
                        'rejected' => 'danger',
                        'expired' => 'gray',
                        default => 'gray',
                    }),

                TextColumn::make('reviewer.name')
                    ->label('Reviewer')
                    ->toggleable(),

                TextColumn::make('submitted_at')
                    ->dateTime()
                    ->sortable()
                    ->toggleable(),

                TextColumn::make('created_at')
                    ->dateTime()
                    ->sortable()
                    ->toggleable(isToggledHiddenByDefault: true),
            ])
            ->filters([
                SelectFilter::make('status')
                    ->options([
                        'draft' => 'Draft',
                        'submitted' => 'Submitted',
                        'under_review' => 'Under Review',
                        'additional_docs_required' => 'Additional Docs Required',
                        'approved' => 'Approved',
                        'rejected' => 'Rejected',
                        'expired' => 'Expired',
                    ]),

                SelectFilter::make('risk_level')
                    ->options([
                        'rendah' => 'Rendah',
                        'menengah_rendah' => 'Menengah Rendah',
                        'menengah_tinggi' => 'Menengah Tinggi',
                        'tinggi' => 'Tinggi',
                    ]),

                SelectFilter::make('business_scale')
                    ->options([
                        'mikro' => 'Mikro',
                        'kecil' => 'Kecil',
                        'menengah' => 'Menengah',
                        'besar' => 'Besar',
                    ]),
            ])
            ->recordActions([
                EditAction::make(),
            ])
            ->toolbarActions([
                BulkActionGroup::make([
                    DeleteBulkAction::make(),
                ]),
            ])
            ->defaultSort('created_at', 'desc');
    }
}
