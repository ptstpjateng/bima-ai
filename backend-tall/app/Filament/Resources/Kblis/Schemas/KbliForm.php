<?php

namespace App\Filament\Resources\Kblis\Schemas;

use Filament\Forms\Components\Select;
use Filament\Forms\Components\Textarea;
use Filament\Forms\Components\TextInput;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Schema;

class KbliForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema->components([

            Section::make('Identitas KBLI')
                ->columns(2)
                ->schema([
                    TextInput::make('kode_kbli')
                        ->label('Kode KBLI')
                        ->required()
                        ->maxLength(10)
                        ->placeholder('03111'),

                    TextInput::make('judul_kbli')
                        ->label('Judul KBLI')
                        ->required()
                        ->maxLength(255),

                    TextInput::make('sektor')
                        ->label('Sektor')
                        ->maxLength(100)
                        ->placeholder('Kelautan dan Perikanan'),

                    Textarea::make('ruang_lingkup')
                        ->label('Ruang Lingkup')
                        ->rows(3)
                        ->columnSpanFull(),
                ]),

            Section::make('Klasifikasi & Perizinan')
                ->columns(2)
                ->schema([
                    Select::make('skala_usaha')
                        ->label('Skala Usaha')
                        ->options([
                            'Mikro' => 'Mikro',
                            'Kecil' => 'Kecil',
                            'Menengah' => 'Menengah',
                            'Besar' => 'Besar',
                            'Menengah dan Besar' => 'Menengah dan Besar',
                            'Seluruh' => 'Seluruh',
                        ])
                        ->searchable(),

                    Select::make('tingkat_risiko')
                        ->label('Tingkat Risiko')
                        ->options([
                            'Rendah' => 'Rendah',
                            'Menengah Rendah' => 'Menengah Rendah',
                            'Menengah Tinggi' => 'Menengah Tinggi',
                            'Tinggi' => 'Tinggi',
                        ]),

                    Textarea::make('perizinan_berusaha')
                        ->label('Perizinan Berusaha')
                        ->rows(2)
                        ->columnSpanFull(),

                    Textarea::make('persyaratan')
                        ->label('Persyaratan')
                        ->rows(3)
                        ->columnSpanFull(),
                ]),

            Section::make('Detail Tambahan')
                ->collapsed()
                ->columns(2)
                ->schema([
                    TextInput::make('jangka_waktu_penerbitan')
                        ->label('Jangka Waktu Penerbitan'),

                    TextInput::make('kewenangan')
                        ->label('Kewenangan'),

                    Textarea::make('kewajiban')
                        ->label('Kewajiban')
                        ->rows(3)
                        ->columnSpanFull(),

                    Textarea::make('pb_umku_names')
                        ->label('PB UMKU Terkait')
                        ->rows(2)
                        ->helperText('Nama PB UMKU, dipisahkan baris baru')
                        ->columnSpanFull(),

                    Textarea::make('parameter')
                        ->label('Parameter')
                        ->rows(2)
                        ->columnSpanFull(),
                ]),
        ]);
    }
}
