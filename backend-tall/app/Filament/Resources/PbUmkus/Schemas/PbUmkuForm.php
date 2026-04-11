<?php

namespace App\Filament\Resources\PbUmkus\Schemas;

use Filament\Forms\Components\Textarea;
use Filament\Forms\Components\TextInput;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Schema;

class PbUmkuForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema->components([

            Section::make('Identitas PB UMKU')
                ->columns(2)
                ->schema([
                    TextInput::make('nomeklatur')
                        ->label('Nomeklatur PB UMKU')
                        ->required()
                        ->maxLength(255)
                        ->columnSpanFull(),

                    Textarea::make('persyaratan')
                        ->label('Persyaratan')
                        ->rows(4)
                        ->columnSpanFull(),
                ]),

            Section::make('Detail Penerbitan')
                ->columns(2)
                ->schema([
                    TextInput::make('jangka_waktu_penerbitan')
                        ->label('Jangka Waktu Penerbitan'),

                    TextInput::make('masa_berlaku')
                        ->label('Masa Berlaku'),

                    Textarea::make('kewajiban')
                        ->label('Kewajiban')
                        ->rows(3)
                        ->columnSpanFull(),

                    Textarea::make('parameter')
                        ->label('Parameter')
                        ->rows(2)
                        ->columnSpanFull(),

                    TextInput::make('kewenangan')
                        ->label('Kewenangan'),
                ]),
        ]);
    }
}
