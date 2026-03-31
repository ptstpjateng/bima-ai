<?php

namespace App\Filament\Resources\AiInteractions\Schemas;

use Filament\Forms\Components\Select;
use Filament\Forms\Components\Textarea;
use Filament\Forms\Components\TextInput;
use Filament\Forms\Components\Toggle;
use Filament\Schemas\Schema;

class AiInteractionForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema
            ->components([
                Select::make('user_id')
                    ->relationship('user', 'name')
                    ->searchable()
                    ->preload()
                    ->nullable(),

                Select::make('permit_application_id')
                    ->relationship('permitApplication', 'application_number')
                    ->searchable()
                    ->preload()
                    ->nullable(),

                TextInput::make('session_id')
                    ->required()
                    ->maxLength(64),

                TextInput::make('turn_index')
                    ->numeric()
                    ->default(0),

                Select::make('channel')
                    ->options([
                        'whatsapp' => 'WhatsApp',
                        'telegram' => 'Telegram',
                        'web' => 'Web',
                        'mobile' => 'Mobile',
                        'internal' => 'Internal',
                    ])
                    ->required()
                    ->default('web'),

                Select::make('message_type')
                    ->options([
                        'user_message' => 'User Message',
                        'ai_response' => 'AI Response',
                        'system_event' => 'System Event',
                    ])
                    ->required(),

                Textarea::make('content')
                    ->required()
                    ->columnSpanFull(),

                Textarea::make('content_rendered')
                    ->columnSpanFull(),

                TextInput::make('intent')
                    ->maxLength(100),

                TextInput::make('model_used')
                    ->maxLength(80),

                TextInput::make('prompt_tokens')
                    ->numeric(),

                TextInput::make('completion_tokens')
                    ->numeric(),

                TextInput::make('response_time_ms')
                    ->numeric()
                    ->suffix('ms'),

                TextInput::make('confidence_score')
                    ->numeric()
                    ->step(0.001)
                    ->minValue(0)
                    ->maxValue(1),

                TextInput::make('external_message_id')
                    ->maxLength(100),

                Toggle::make('is_flagged')
                    ->label('Flagged for review'),

                TextInput::make('flag_reason')
                    ->maxLength(200),

                Toggle::make('is_sensitive')
                    ->label('Contains sensitive data'),
            ]);
    }
}
