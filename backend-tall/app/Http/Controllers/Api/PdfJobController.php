<?php

namespace App\Http\Controllers\Api;

use App\Models\PdfParseJob;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Validation\ValidationException;

/**
 * Internal PDF parsing job endpoints — protected by X-Internal-Key.
 *
 * Used by the data-pipeline container (pdf_agent_pipeline.py) to:
 *   GET   /api/internal/pdf-jobs/{id}            — fetch job details & file paths
 *   PATCH /api/internal/pdf-jobs/{id}/progress   — update progress percentage
 *   POST  /api/internal/pdf-jobs/{id}/complete   — mark job completed with results
 *   POST  /api/internal/pdf-jobs/{id}/fail       — mark job failed with error
 */
class PdfJobController extends ApiController
{
    /**
     * GET /api/internal/pdf-jobs/{id}
     * Returns the job record so the pipeline knows which files to process.
     */
    public function show(Request $request, int $id): JsonResponse
    {
        try {
            $this->assertInternalKey($request);

            $job = PdfParseJob::findOrFail($id);

            return $this->success([
                'id'               => $job->id,
                'name'             => $job->name,
                'kbli_pdf_path'    => $job->kbli_pdf_path,
                'pb_umku_pdf_path' => $job->pb_umku_pdf_path,
                'kbli_codes_filter'=> $job->kbli_codes_filter,
                'status'           => $job->status,
            ]);

        } catch (\Illuminate\Database\Eloquent\ModelNotFoundException) {
            return $this->error("Job #{$id} tidak ditemukan.", 404, 'NOT_FOUND');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'PdfJobController@show');
        }
    }

    /**
     * PATCH /api/internal/pdf-jobs/{id}/progress
     * Body: { pct: 0–100 }
     */
    public function updateProgress(Request $request, int $id): JsonResponse
    {
        try {
            $this->assertInternalKey($request);

            $validated = $request->validate([
                'pct' => ['required', 'integer', 'min:0', 'max:100'],
            ]);

            $job = PdfParseJob::findOrFail($id);
            $job->updateProgress($validated['pct']);

            return $this->success(['id' => $id, 'progress_pct' => $validated['pct']]);

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Illuminate\Database\Eloquent\ModelNotFoundException) {
            return $this->error("Job #{$id} tidak ditemukan.", 404, 'NOT_FOUND');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'PdfJobController@updateProgress');
        }
    }

    /**
     * POST /api/internal/pdf-jobs/{id}/complete
     * Body: { chunks_created, kbli_processed, result_json? }
     */
    public function complete(Request $request, int $id): JsonResponse
    {
        try {
            $this->assertInternalKey($request);

            $validated = $request->validate([
                'chunks_created'  => ['required', 'integer', 'min:0'],
                'kbli_processed'  => ['required', 'integer', 'min:0'],
                'result_json'     => ['nullable', 'string'],
            ]);

            $job = PdfParseJob::findOrFail($id);
            $job->markCompleted(
                $validated['chunks_created'],
                $validated['kbli_processed'],
                $validated['result_json'] ?? null,
            );

            return $this->success([
                'id'              => $id,
                'status'          => 'completed',
                'chunks_created'  => $validated['chunks_created'],
                'kbli_processed'  => $validated['kbli_processed'],
            ], 200, 'Job selesai dengan sukses.');

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Illuminate\Database\Eloquent\ModelNotFoundException) {
            return $this->error("Job #{$id} tidak ditemukan.", 404, 'NOT_FOUND');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'PdfJobController@complete');
        }
    }

    /**
     * POST /api/internal/pdf-jobs/{id}/fail
     * Body: { error: string }
     */
    public function fail(Request $request, int $id): JsonResponse
    {
        try {
            $this->assertInternalKey($request);

            $validated = $request->validate([
                'error' => ['required', 'string', 'max:2000'],
            ]);

            $job = PdfParseJob::findOrFail($id);
            $job->markFailed($validated['error']);

            return $this->success(['id' => $id, 'status' => 'failed']);

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Illuminate\Database\Eloquent\ModelNotFoundException) {
            return $this->error("Job #{$id} tidak ditemukan.", 404, 'NOT_FOUND');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'PdfJobController@fail');
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
