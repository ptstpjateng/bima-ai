<?php

namespace App\Http\Controllers\Api;

use App\Models\KbliScrapeTarget;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Validation\ValidationException;

/**
 * Internal pipeline endpoints — protected by X-Internal-Key header.
 *
 * Used by the data-pipeline container to:
 *   GET  /api/internal/pipeline/queue    — fetch next batch of pending KBLI codes
 *   POST /api/internal/pipeline/status   — report back scrape result per code
 */
class PipelineController extends ApiController
{
    /**
     * GET /api/internal/pipeline/queue
     *
     * Returns up to `limit` (default 10, max 50) pending targets ordered by
     * priority DESC, then oldest-first. Atomically transitions them to 'queued'
     * so a second caller cannot pick up the same batch.
     */
    public function queue(Request $request): JsonResponse
    {
        try {
            $this->assertInternalKey($request);

            $limit = (int) min((int) $request->query('limit', 10), 50);

            $targets = KbliScrapeTarget::where('status', 'pending')
                ->orderByDesc('priority')
                ->orderBy('updated_at')
                ->limit($limit)
                ->get(['id', 'kbli_code', 'kbli_description', 'sector', 'priority']);

            if ($targets->isNotEmpty()) {
                KbliScrapeTarget::whereIn('id', $targets->pluck('id'))
                    ->update(['status' => 'queued']);
            }

            return $this->success([
                'count'   => $targets->count(),
                'targets' => $targets->map(fn ($t) => [
                    'kbli_code'        => $t->kbli_code,
                    'kbli_description' => $t->kbli_description,
                    'sector'           => $t->sector,
                    'priority'         => $t->priority,
                ]),
            ]);

        } catch (\Throwable $e) {
            if ($e instanceof \Illuminate\Auth\Access\AuthorizationException || str_contains($e->getMessage(), 'FORBIDDEN')) {
                return $this->error('Akses ditolak.', 403, 'FORBIDDEN');
            }
            return $this->serverError($e, 'PipelineController@queue');
        }
    }

    /**
     * POST /api/internal/pipeline/status
     *
     * Body: { kbli_code, status (scraping|done|failed), chunks_upserted?, duration_seconds?, error? }
     */
    public function updateStatus(Request $request): JsonResponse
    {
        try {
            $this->assertInternalKey($request);

            $validated = $request->validate([
                'kbli_code'        => ['required', 'string', 'max:10'],
                'status'           => ['required', 'in:scraping,done,failed'],
                'chunks_upserted'  => ['nullable', 'integer', 'min:0'],
                'duration_seconds' => ['nullable', 'integer', 'min:0'],
                'error'            => ['nullable', 'string', 'max:2000'],
            ]);

            $target = KbliScrapeTarget::where('kbli_code', $validated['kbli_code'])->first();

            if (! $target) {
                // Auto-create unknown codes so the pipeline can report back even
                // for codes not pre-registered in the admin panel.
                $target = KbliScrapeTarget::create([
                    'kbli_code' => $validated['kbli_code'],
                    'status'    => 'pending',
                ]);
            }

            match ($validated['status']) {
                'scraping' => $target->markScraping(),
                'done'     => $target->markDone(
                    $validated['chunks_upserted'] ?? 0,
                    $validated['duration_seconds'] ?? 0
                ),
                'failed'   => $target->markFailed($validated['error'] ?? 'Unknown error'),
            };

            return $this->success([
                'kbli_code' => $target->kbli_code,
                'status'    => $target->status,
            ], 200, 'Status diperbarui.');

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Throwable $e) {
            if (str_contains($e->getMessage(), 'FORBIDDEN')) {
                return $this->error('Akses ditolak.', 403, 'FORBIDDEN');
            }
            return $this->serverError($e, 'PipelineController@updateStatus');
        }
    }

    // ── Private ───────────────────────────────────────────────────────────────

    private function assertInternalKey(Request $request): void
    {
        $internalKey = config('app.internal_api_key');
        $provided    = $request->header('X-Internal-Key');

        if (! $internalKey || ! $provided || ! hash_equals($internalKey, (string) $provided)) {
            throw new \RuntimeException('FORBIDDEN');
        }
    }
}
