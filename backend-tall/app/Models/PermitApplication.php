<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\SoftDeletes;

class PermitApplication extends Model
{
    use SoftDeletes;

    protected $fillable = [
        'user_id',
        'reviewer_id',
        'application_number',
        'oss_application_number',
        'nib',
        'kbli_code',
        'kbli_description',
        'kbli_section',
        'business_scale',
        'risk_level',
        'status',
        'business_location_address',
        'business_location_province',
        'business_location_city',
        'annual_revenue_estimate',
        'employee_count',
        'documents',
        'requirements_checklist',
        'reviewer_notes',
        'applicant_notes',
        'submitted_at',
        'reviewed_at',
        'approved_at',
        'rejected_at',
        'permit_expiry_date',
        'metadata',
    ];

    protected $casts = [
        'documents' => 'array',
        'requirements_checklist' => 'array',
        'metadata' => 'array',
        'submitted_at' => 'datetime',
        'reviewed_at' => 'datetime',
        'approved_at' => 'datetime',
        'rejected_at' => 'datetime',
        'permit_expiry_date' => 'date',
    ];

    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }

    public function reviewer(): BelongsTo
    {
        return $this->belongsTo(User::class, 'reviewer_id');
    }
}
