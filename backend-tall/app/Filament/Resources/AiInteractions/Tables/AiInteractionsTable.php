<?php

namespace App\Filament\Resources\AiInteractions\Tables;

use App\Models\AiInteraction;
use Filament\Actions\BulkActionGroup;
use Filament\Actions\DeleteBulkAction;
use Filament\Actions\EditAction;
use Filament\Tables\Actions\Action;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Filters\Filter;
use Filament\Tables\Filters\SelectFilter;
use Filament\Tables\Filters\TernaryFilter;
use Filament\Tables\Table;
use Illuminate\Database\Eloquent\Builder;

class AiInteractionsTable
{
    public static function configure(Table $table): Table
    {
        return $table
            ->columns([
                TextColumn::make('created_at')
                    ->label('Waktu')
                    ->dateTime('d M H:i')
                    ->sortable()
                    ->width('120px'),

                TextColumn::make('user.name')
                    ->label('Pengguna')
                    ->searchable()
                    ->sortable()
                    ->placeholder('Anonim')
                    ->limit(20),

                TextColumn::make('channel')
                    ->label('Kanal')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'whatsapp' => 'success',
                        'telegram' => 'info',
                        'web'      => 'primary',
                        'mobile'   => 'warning',
                        'internal' => 'gray',
                        default    => 'gray',
                    })
                    ->formatStateUsing(fn (string $state): string => match ($state) {
                        'whatsapp' => 'WhatsApp',
                        'telegram' => 'Telegram',
                        'web'      => 'Web',
                        'mobile'   => 'Mobile',
                        default    => $state,
                    }),

                TextColumn::make('message_type')
                    ->label('Tipe')
                    ->badge()
                    ->color(fn (string $state): string => match ($state) {
                        'user_message' => 'info',
                        'ai_response'  => 'success',
                        'system_event' => 'gray',
                        default        => 'gray',
                    })
                    ->formatStateUsing(fn (string $state): string => match ($state) {
                        'user_message' => 'User',
                        'ai_response'  => 'AI',
                        'system_event' => 'System',
                        default        => $state,
                    }),

                TextColumn::make('intent')
                    ->label('Intent')
                    ->badge()
                    ->color('gray')
                    ->placeholder('—')
                    ->limit(25)
                    ->toggleable(),

                TextColumn::make('content')
                    ->label('Pesan')
                    ->limit(80)
                    ->tooltip(fn ($state) => $state)
                    ->searchable()
                    ->wrap(),

                TextColumn::make('session_id')
                    ->label('Sesi')
                    ->copyable()
                    ->limit(16)
                    ->searchable()
                    ->toggleable(isToggledHiddenByDefault: true),

                TextColumn::make('response_time_ms')
                    ->label('Latensi')
                    ->formatStateUsing(fn ($state) => $state ? "{$state}ms" : '—')
                    ->color(fn ($state) => match (true) {
                        $state === null => 'gray',
                        $state > 10000  => 'danger',
                        $state > 4000   => 'warning',
                        default         => 'success',
                    })
                    ->alignEnd()
                    ->toggleable(),
            ])
            ->filters([
                SelectFilter::make('channel')
                    ->label('Kanal')
                    ->options([
                        'whatsapp' => 'WhatsApp',
                        'telegram' => 'Telegram',
                        'web'      => 'Web',
                        'mobile'   => 'Mobile',
                        'internal' => 'Internal',
                    ]),

                SelectFilter::make('message_type')
                    ->label('Tipe Pesan')
                    ->options([
                        'user_message' => 'Pesan Pengguna',
                        'ai_response'  => 'Respons AI',
                        'system_event' => 'System Event',
                    ]),

                // Thread view: filter by session_id to see full conversation
                Filter::make('session')
                    ->label('Lihat Thread Sesi')
                    ->form([
                        \Filament\Forms\Components\TextInput::make('session_id')
                            ->label('Session ID')
                            ->placeholder('Masukkan session ID…'),
                    ])
                    ->query(fn (Builder $query, array $data) =>
                        $query->when(
                            filled($data['session_id']),
                            fn ($q) => $q->where('session_id', $data['session_id'])
                                        ->orderBy('turn_index'),
                        )
                    )
                    ->indicateUsing(fn (array $data) =>
                        filled($data['session_id'])
                            ? 'Thread: ' . $data['session_id']
                            : null
                    ),

                TernaryFilter::make('is_flagged')
                    ->label('Ditandai'),

                Filter::make('today')
                    ->label('Hari ini saja')
                    ->query(fn (Builder $q) => $q->whereDate('created_at', today()))
                    ->toggle(),
            ])
            ->recordActions([
                EditAction::make()->label(''),
                // Quick-link to view the entire session thread
                Action::make('view_session')
                    ->label('')
                    ->tooltip('Lihat seluruh thread sesi ini')
                    ->icon('heroicon-o-chat-bubble-left-right')
                    ->color('gray')
                    ->url(fn (AiInteraction $record) =>
                        '/admin/ai-interactions?' .
                        http_build_query(['tableFilters' => [
                            'session' => ['session_id' => $record->session_id],
                        ]])
                    ),
            ])
            ->toolbarActions([
                BulkActionGroup::make([
                    DeleteBulkAction::make(),
                ]),
            ])
            ->defaultSort('created_at', 'desc')
            ->striped()
            ->poll('30s');
    }
}
