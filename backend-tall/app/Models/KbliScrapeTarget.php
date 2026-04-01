<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Builder;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class KbliScrapeTarget extends Model
{
    use HasFactory;

    protected $fillable = [
        'kbli_code',
        'kbli_description',
        'sector',
        'status',
        'priority',
        'last_scraped_at',
        'scrape_duration_seconds',
        'chroma_chunks',
        'scrape_error',
        'created_by',
    ];

    protected $casts = [
        'last_scraped_at'         => 'datetime',
        'priority'                => 'integer',
        'chroma_chunks'           => 'integer',
        'scrape_duration_seconds' => 'integer',
    ];

    // ── Relations ─────────────────────────────────────────────────────────────

    public function creator(): BelongsTo
    {
        return $this->belongsTo(User::class, 'created_by');
    }

    // ── Scopes ────────────────────────────────────────────────────────────────

    public function scopePending(Builder $query): Builder
    {
        return $query->where('status', 'pending');
    }

    public function scopeDone(Builder $query): Builder
    {
        return $query->where('status', 'done');
    }

    public function scopeFailed(Builder $query): Builder
    {
        return $query->where('status', 'failed');
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    public function markScraping(): void
    {
        $this->update(['status' => 'scraping', 'scrape_error' => null]);
    }

    public function markDone(int $chunks, int $durationSeconds): void
    {
        $this->update([
            'status'                  => 'done',
            'chroma_chunks'           => $chunks,
            'scrape_duration_seconds' => $durationSeconds,
            'last_scraped_at'         => now(),
            'scrape_error'            => null,
        ]);
    }

    public function markFailed(string $error): void
    {
        $this->update([
            'status'          => 'failed',
            'scrape_error'    => $error,
            'last_scraped_at' => now(),
        ]);
    }

    public function requeue(): void
    {
        $this->update([
            'status'      => 'pending',
            'scrape_error' => null,
        ]);
    }
}
