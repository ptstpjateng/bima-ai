<?php

namespace App\Filament\Resources\KbliScrapeTargets\Tables;

use App\Models\KbliScrapeTarget;
use Filament\Actions\Action;
use Filament\Actions\BulkActionGroup;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\EditAction;
use Filament\Tables\Columns\IconColumn;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Filters\SelectFilter;
use Filament\Tables\Table;

class KbliScrapeTargetsTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->columns([
                TextColumn::make('kbli_code')
                    ->label('Kode KBLI')
                    ->searchable()
                    ->sortable()
                    ->weight('bold')
                    ->copyable(),

                TextColumn::make('kbli_description')
                    ->label('Deskripsi')
                    ->searchable()
                    ->limit(40)
                    ->placeholder('—'),

                TextColumn::make('sector')
                    ->label('Sektor')
                    ->badge()
                    ->color('gray')
                    ->placeholder('—'),

                TextColumn::make('status')
                    ->label('Status')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'pending'  => 'gray',
                        'queued'   => 'info',
                        'scraping' => 'warning',
                        'done'     => 'success',
                        'failed'   => 'danger',
                        default    => 'gray',
                    })
                    ->formatStateUsing(fn (string $state): string => match ($state) {
                        'pending'  => 'Menunggu',
                        'queued'   => 'Dalam Antrian',
                        'scraping' => 'Sedang Diproses',
                        'done'     => 'Selesai',
                        'failed'   => 'Gagal',
                        default    => $state,
                    })
                    ->sortable(),

                TextColumn::make('chroma_chunks')
                    ->label('Chunks')
                    ->numeric()
                    ->sortable()
                    ->placeholder('—')
                    ->alignCenter(),

                TextColumn::make('priority')
                    ->label('Prioritas')
                    ->sortable()
                    ->alignCenter()
                    ->toggleable(isToggledHiddenByDefault: true),

                TextColumn::make('last_scraped_at')
                    ->label('Terakhir Diproses')
                    ->since()
                    ->sortable()
                    ->placeholder('Belum pernah'),

                TextColumn::make('scrape_error')
                    ->label('Error')
                    ->limit(50)
                    ->tooltip(fn ($state) => $state)
                    ->color('danger')
                    ->placeholder('—')
                    ->toggleable(isToggledHiddenByDefault: true),
            ])
            ->filters([
                SelectFilter::make('status')
                    ->label('Status')
                    ->options([
                        'pending'  => 'Menunggu',
                        'queued'   => 'Dalam Antrian',
                        'scraping' => 'Sedang Diproses',
                        'done'     => 'Selesai',
                        'failed'   => 'Gagal',
                    ]),

                SelectFilter::make('sector')
                    ->label('Sektor')
                    ->options([
                        'Food & Beverage'      => 'Makanan & Minuman',
                        'Retail & Trade'       => 'Perdagangan & Ritel',
                        'Health Services'      => 'Layanan Kesehatan',
                        'IT & Technology'      => 'Teknologi & Informasi',
                        'Construction'         => 'Konstruksi & Bangunan',
                        'Beauty & Personal'    => 'Kecantikan & Perawatan',
                        'Education & Training' => 'Pendidikan & Pelatihan',
                        'Transportation'       => 'Transportasi & Logistik',
                        'Manufacturing'        => 'Manufaktur & Industri',
                        'Accommodation'        => 'Akomodasi & Perhotelan',
                        'Agriculture'          => 'Pertanian & Perkebunan',
                    ]),
            ])
            ->actions([
                Action::make('requeue')
                    ->label('Re-queue')
                    ->icon('heroicon-o-arrow-path')
                    ->color('warning')
                    ->requiresConfirmation()
                    ->modalHeading('Re-queue Scraping?')
                    ->modalDescription(fn ($record) => "Jadwalkan ulang scraping untuk KBLI {$record->kbli_code}? Status saat ini akan direset ke Menunggu.")
                    ->action(fn (KbliScrapeTarget $record) => $record->requeue())
                    ->visible(fn (KbliScrapeTarget $record) => in_array($record->status, ['done', 'failed', 'queued'])),

                EditAction::make(),
            ])
            ->bulkActions([
                BulkActionGroup::make([
                    \Filament\Actions\BulkAction::make('bulk_requeue')
                        ->label('Re-queue Terpilih')
                        ->icon('heroicon-o-arrow-path')
                        ->color('warning')
                        ->requiresConfirmation()
                        ->action(fn ($records) => $records->each->requeue()),

                    DeleteBulkAction::make(),
                ]),
            ])
            ->defaultSort('priority', 'desc')
            ->poll('15s');
    }
}
