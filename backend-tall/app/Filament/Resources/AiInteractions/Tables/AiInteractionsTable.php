<?php

namespace App\Filament\Resources\AiInteractions\Tables;

use Filament\Actions\BulkActionGroup;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\EditAction;
use Filament\Tables\Columns\IconColumn;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Filters\SelectFilter;
use Filament\Tables\Filters\TernaryFilter;
use Filament\Tables\Table;

class AiInteractionsTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->columns([
                TextColumn::make('session_id')
                    ->searchable()
                    ->limit(20),

                TextColumn::make('user.name')
                    ->label('User')
                    ->searchable()
                    ->sortable(),

                TextColumn::make('channel')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'whatsapp' => 'success',
                        'telegram' => 'info',
                        'web' => 'primary',
                        'mobile' => 'warning',
                        'internal' => 'gray',
                        default => 'gray',
                    }),

                TextColumn::make('message_type')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'user_message' => 'info',
                        'ai_response' => 'success',
                        'system_event' => 'gray',
                        default => 'gray',
                    }),

                TextColumn::make('content')
                    ->limit(60)
                    ->searchable(),

                TextColumn::make('intent')
                    ->searchable()
                    ->toggleable(),

                TextColumn::make('model_used')
                    ->toggleable(),

                IconColumn::make('is_flagged')
                    ->boolean()
                    ->label('Flagged'),

                IconColumn::make('is_sensitive')
                    ->boolean()
                    ->label('Sensitive')
                    ->toggleable(),

                TextColumn::make('created_at')
                    ->dateTime()
                    ->sortable(),
            ])
            ->filters([
                SelectFilter::make('channel')
                    ->options([
                        'whatsapp' => 'WhatsApp',
                        'telegram' => 'Telegram',
                        'web' => 'Web',
                        'mobile' => 'Mobile',
                        'internal' => 'Internal',
                    ]),

                SelectFilter::make('message_type')
                    ->options([
                        'user_message' => 'User Message',
                        'ai_response' => 'AI Response',
                        'system_event' => 'System Event',
                    ]),

                TernaryFilter::make('is_flagged')
                    ->label('Flagged'),
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
