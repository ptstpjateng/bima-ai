<?php

namespace App\Filament\Widgets;

use App\Models\AiInteraction;
use Filament\Tables\Columns\BadgeColumn;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Table;
use Filament\Widgets\TableWidget as BaseWidget;

class LatestAiInteractionsWidget extends BaseWidget
{
    protected static ?int $sort = 3;

    protected int | string | array $columnSpan = 'full';

    protected static ?string $heading = 'Aktivitas AI Terbaru';

    public function table(Table $table): Table
    {
        return $table
            ->query(
                AiInteraction::query()
                    ->where('message_type', 'ai_response')
                    ->latest()
                    ->limit(8)
            )
            ->columns([
                TextColumn::make('created_at')
                    ->label('Waktu')
                    ->dateTime('d M, H:i')
                    ->sortable(),

                TextColumn::make('user.name')
                    ->label('Pengguna')
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
                        default    => 'gray',
                    })
                    ->formatStateUsing(fn (string $state): string => match ($state) {
                        'whatsapp' => '💬 WhatsApp',
                        'telegram' => '✈️ Telegram',
                        'web'      => '🌐 Web',
                        'mobile'   => '📱 Mobile',
                        default    => $state,
                    }),

                TextColumn::make('intent')
                    ->label('Intent')
                    ->badge()
                    ->color('gray')
                    ->placeholder('—')
                    ->limit(30),

                TextColumn::make('content')
                    ->label('Respons AI')
                    ->limit(80)
                    ->tooltip(fn ($state) => $state)
                    ->wrap(),

                TextColumn::make('response_time_ms')
                    ->label('Latensi')
                    ->formatStateUsing(fn ($state) => $state ? "{$state}ms" : '—')
                    ->color(fn ($state) => match(true) {
                        $state === null  => 'gray',
                        $state > 5000    => 'danger',
                        $state > 2000    => 'warning',
                        default          => 'success',
                    })
                    ->alignEnd(),
            ])
            ->paginated(false)
            ->striped();
    }
}
