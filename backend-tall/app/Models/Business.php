<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\SoftDeletes;

class Business extends Model
{
    use HasFactory, SoftDeletes;

    protected $fillable = [
        'user_id',
        'name',
        'legal_type',
        'registration_number',
        'primary_kbli_code',
        'primary_kbli_description',
        'additional_kbli_codes',
        'scale',
        'annual_revenue_estimate',
        'employee_count',
        'address',
        'province',
        'city',
        'district',
        'postal_code',
        'is_primary',
        'metadata',
    ];

    protected $casts = [
        'additional_kbli_codes' => 'array',
        'metadata'              => 'array',
        'is_primary'            => 'boolean',
    ];

    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }
}
