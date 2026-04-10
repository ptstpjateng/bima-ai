<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class PdfParseJob extends Model
{
    protected $fillable = [
        'name',
        'kbli_pdf_path',
        'pb_umku_pdf_path',
        'kbli_codes_filter',
        'status',
        'progress_pct',
        'chunks_created',
        'kbli_processed',
        'result_json',
        'error_message',
        'created_by',
        'started_at',
        'completed_at',
    ];

    protected $casts = [
        'started_at'   => 'datetime',
        'completed_at' => 'datetime',
    ];

    public function creator(): BelongsTo
    {
        return $this->belongsTo(User::class, 'created_by');
    }

    // ── Status transitions ───────────────────────────────────────────────────

    public function markProcessing(): void
    {
        $this->update([
            'status'     => 'processing',
            'started_at' => now(),
            'progress_pct' => 0,
            'error_message' => null,
        ]);
    }

    public function updateProgress(int $pct): void
    {
        $this->update(['progress_pct' => max(0, min(100, $pct))]);
    }

    public function markCompleted(int $chunks, int $kbliProcessed, ?string $resultJson = null): void
    {
        $this->update([
            'status'         => 'completed',
            'progress_pct'   => 100,
            'chunks_created' => $chunks,
            'kbli_processed' => $kbliProcessed,
            'result_json'    => $resultJson,
            'completed_at'   => now(),
            'error_message'  => null,
        ]);
    }

    public function markFailed(string $error): void
    {
        $this->update([
            'status'        => 'failed',
            'error_message' => substr($error, 0, 2000),
            'completed_at'  => now(),
        ]);
    }

    public function reset(): void
    {
        $this->update([
            'status'         => 'pending',
            'progress_pct'   => 0,
            'chunks_created' => 0,
            'kbli_processed' => 0,
            'result_json'    => null,
            'error_message'  => null,
            'started_at'     => null,
            'completed_at'   => null,
        ]);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    public function isRunning(): bool
    {
        return $this->status === 'processing';
    }

    public function statusColor(): string
    {
        return match ($this->status) {
            'pending'    => 'gray',
            'processing' => 'warning',
            'completed'  => 'success',
            'failed'     => 'danger',
            default      => 'gray',
        };
    }

    public function statusLabel(): string
    {
        return match ($this->status) {
            'pending'    => 'Menunggu',
            'processing' => 'Diproses',
            'completed'  => 'Selesai ✓',
            'failed'     => 'Gagal ✗',
            default      => $this->status,
        };
    }
}
