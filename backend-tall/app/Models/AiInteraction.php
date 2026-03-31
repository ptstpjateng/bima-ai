<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class AiInteraction extends Model
{
    protected $fillable = [
        'user_id',
        'permit_application_id',
        'session_id',
        'turn_index',
        'channel',
        'message_type',
        'content',
        'content_rendered',
        'intent',
        'entities',
        'context_snapshot',
        'model_used',
        'prompt_tokens',
        'completion_tokens',
        'response_time_ms',
        'confidence_score',
        'external_message_id',
        'is_flagged',
        'flag_reason',
        'is_sensitive',
    ];

    protected $casts = [
        'entities' => 'array',
        'context_snapshot' => 'array',
        'is_flagged' => 'boolean',
        'is_sensitive' => 'boolean',
    ];

    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }

    public function permitApplication(): BelongsTo
    {
        return $this->belongsTo(PermitApplication::class);
    }
}
