<?php

namespace App\Filament\Resources\PermitApplications\Schemas;

use Filament\Forms\Components\DatePicker;
use Filament\Forms\Components\DateTimePicker;
use Filament\Forms\Components\Select;
use Filament\Forms\Components\Textarea;
use Filament\Forms\Components\TextInput;
use Filament\Schemas\Schema;

class PermitApplicationForm
{
    public static function configure(Schema $schema): Schema
    {
        return $schema
            ->components([
                Select::make('user_id')
                    ->relationship('user', 'name')
                    ->searchable()
                    ->preload()
                    ->required(),

                Select::make('reviewer_id')
                    ->relationship('reviewer', 'name')
                    ->searchable()
                    ->preload()
                    ->nullable(),

                TextInput::make('application_number')
                    ->required()
                    ->unique(ignoreRecord: true)
                    ->maxLength(30),

                TextInput::make('oss_application_number')
                    ->maxLength(50),

                TextInput::make('nib')
                    ->maxLength(20),

                TextInput::make('kbli_code')
                    ->required()
                    ->maxLength(10),

                TextInput::make('kbli_description')
                    ->required(),

                TextInput::make('kbli_section')
                    ->maxLength(100),

                Select::make('business_scale')
                    ->options([
                        'mikro' => 'Mikro',
                        'kecil' => 'Kecil',
                        'menengah' => 'Menengah',
                        'besar' => 'Besar',
                    ])
                    ->required()
                    ->default('mikro'),

                Select::make('risk_level')
                    ->options([
                        'rendah' => 'Rendah',
                        'menengah_rendah' => 'Menengah Rendah',
                        'menengah_tinggi' => 'Menengah Tinggi',
                        'tinggi' => 'Tinggi',
                    ])
                    ->required()
                    ->default('rendah'),

                Select::make('status')
                    ->options([
                        'draft' => 'Draft',
                        'submitted' => 'Submitted',
                        'under_review' => 'Under Review',
                        'additional_docs_required' => 'Additional Docs Required',
                        'approved' => 'Approved',
                        'rejected' => 'Rejected',
                        'expired' => 'Expired',
                    ])
                    ->required()
                    ->default('draft'),

                Textarea::make('business_location_address')
                    ->columnSpanFull(),

                TextInput::make('business_location_province')
                    ->maxLength(100),

                TextInput::make('business_location_city')
                    ->maxLength(100),

                TextInput::make('annual_revenue_estimate')
                    ->numeric()
                    ->prefix('Rp'),

                TextInput::make('employee_count')
                    ->numeric(),

                Textarea::make('reviewer_notes')
                    ->columnSpanFull(),

                Textarea::make('applicant_notes')
                    ->columnSpanFull(),

                DateTimePicker::make('submitted_at'),
                DateTimePicker::make('reviewed_at'),
                DateTimePicker::make('approved_at'),
                DateTimePicker::make('rejected_at'),
                DatePicker::make('permit_expiry_date'),
            ]);
    }
}
