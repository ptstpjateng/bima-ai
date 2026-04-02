<?php

namespace App\Services;

use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;

class PipelineService
{
    private string $aiEngineUrl;

    public function __construct()
    {
        $this->aiEngineUrl = config('services.ai_engine.url', env('AI_ENGINE_URL', 'http://ai-engine:8000'));
    }

    /**
     * Trigger a pipeline run for up to $limit pending KBLI targets.
     * Non-blocking — the scraper runs in the background.
     */
    public function trigger(int $limit = 35): array
    {
        try {
            $response = Http::timeout(5)->post("{$this->aiEngineUrl}/pipeline/trigger", [
                'limit' => $limit,
            ]);

            if ($response->status() === 409) {
                return ['success' => true, 'message' => 'Pipeline sudah berjalan.', 'already_running' => true];
            }

            if ($response->successful()) {
                return ['success' => true, 'message' => $response->json('message', 'Pipeline dimulai.'), 'already_running' => false];
            }

            Log::warning('PipelineService::trigger failed', ['status' => $response->status(), 'body' => $response->body()]);
            return ['success' => false, 'message' => "HTTP {$response->status()}: {$response->body()}"];

        } catch (\Throwable $e) {
            Log::error('PipelineService::trigger exception', ['error' => $e->getMessage()]);
            return ['success' => false, 'message' => $e->getMessage()];
        }
    }

    /**
     * Fetch all ChromaDB chunks for a specific KBLI code.
     */
    public function getChunks(string $kbliCode): array
    {
        try {
            $response = Http::timeout(10)->get("{$this->aiEngineUrl}/chunks", [
                'kbli_code' => $kbliCode,
            ]);

            if ($response->successful()) {
                return $response->json('chunks', []);
            }
        } catch (\Throwable $e) {
            Log::warning('PipelineService::getChunks failed', ['kbli_code' => $kbliCode, 'error' => $e->getMessage()]);
        }

        return [];
    }
}
