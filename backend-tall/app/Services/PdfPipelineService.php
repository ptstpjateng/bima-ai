<?php

namespace App\Services;

use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;

/**
 * Calls the data-pipeline container's FastAPI server to trigger
 * the multi-agent PDF parsing pipeline for a specific job.
 *
 * The pipeline server is at http://data-pipeline:9000 (bima-internal network).
 */
class PdfPipelineService
{
    private string $pipelineUrl;

    public function __construct()
    {
        // data-pipeline container runs on port 9000 within bima-internal network
        $this->pipelineUrl = rtrim(env('DATA_PIPELINE_URL', 'http://data-pipeline:9000'), '/');
    }

    /**
     * Trigger the PDF parsing pipeline for a specific job ID.
     * Non-blocking: the pipeline runs asynchronously in the data-pipeline container.
     *
     * @return array{ success: bool, message: string }
     */
    public function triggerPdfJob(int $jobId): array
    {
        try {
            $response = Http::timeout(10)->post("{$this->pipelineUrl}/pipeline/pdf/{$jobId}");

            if ($response->status() === 409) {
                return ['success' => true, 'message' => 'Pipeline PDF sedang berjalan untuk job ini.', 'already_running' => true];
            }

            if ($response->successful()) {
                return ['success' => true, 'message' => $response->json('message', 'Pipeline PDF dimulai.'), 'already_running' => false];
            }

            Log::warning('PdfPipelineService::triggerPdfJob failed', [
                'job_id' => $jobId,
                'status' => $response->status(),
                'body'   => $response->body(),
            ]);

            return ['success' => false, 'message' => "HTTP {$response->status()}: Pipeline gagal dimulai."];

        } catch (\Throwable $e) {
            Log::error('PdfPipelineService::triggerPdfJob exception', [
                'job_id' => $jobId,
                'error'  => $e->getMessage(),
            ]);
            return ['success' => false, 'message' => "Tidak dapat menghubungi pipeline server: {$e->getMessage()}"];
        }
    }

    /**
     * Check if the pipeline server is healthy.
     */
    public function isHealthy(): bool
    {
        try {
            $response = Http::timeout(3)->get("{$this->pipelineUrl}/health");
            return $response->successful();
        } catch (\Throwable) {
            return false;
        }
    }
}
