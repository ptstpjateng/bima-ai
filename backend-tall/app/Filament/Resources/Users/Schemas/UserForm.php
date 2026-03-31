<?php

namespace App\Filament\Resources\Users\Schemas;

use Filament\Forms\Components\DateTimePicker;
use Filament\Forms\Components\Select;
use Filament\Forms\Components\TextInput;
use Filament\Forms\Components\Textarea;
use Filament\Schemas\Schema;

class UserForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema
            ->components([
                TextInput::make('name')
                    ->required(),

                TextInput::make('email')
                    ->label('Email address')
                    ->email()
                    ->required()
                    ->unique(ignoreRecord: true),

                DateTimePicker::make('email_verified_at')
                    ->label('Email verified at'),

                TextInput::make('password')
                    ->password()
                    ->dehydrateStateUsing(fn ($state) => filled($state) ? bcrypt($state) : null)
                    ->dehydrated(fn ($state) => filled($state))
                    ->required(fn (string $operation) => $operation === 'create'),

                Select::make('role')
                    ->options([
                        'admin' => 'Admin',
                        'staff' => 'Staff',
                        'msme' => 'MSME',
                    ])
                    ->required()
                    ->default('msme'),

                TextInput::make('phone')
                    ->tel(),

                TextInput::make('nik')
                    ->maxLength(16),

                TextInput::make('npwp')
                    ->maxLength(20),

                TextInput::make('business_name'),

                TextInput::make('business_legal_type'),

                Textarea::make('business_address')
                    ->columnSpanFull(),

                TextInput::make('province'),

                TextInput::make('city'),

                TextInput::make('district'),

                TextInput::make('village'),

                TextInput::make('postal_code')
                    ->maxLength(10),

                TextInput::make('profile_photo_path'),
            ]);
    }
}
