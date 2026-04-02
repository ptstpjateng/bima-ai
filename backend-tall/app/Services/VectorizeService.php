<?php

namespace App\Services;

use App\Models\KnowledgeBaseArticle;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;

class VectorizeService
{
    private string $aiEngineUrl;

    public function __construct()
    {
        $this->aiEngineUrl = config('services.ai_engine.url', env('AI_ENGINE_URL', 'http://ai-engine:8000'));
    }

    /**
     * Upsert the article into ChromaDB via the AI engine.
     * Works for both first-time vectorization and forced re-sync.
     * Returns ['success' => bool, 'doc_id' => string|null, 'error' => string|null].
     */
    public function sync(KnowledgeBaseArticle $article): array
    {
        $docId = $article->chroma_doc_id ?? 'kb-' . $article->id . '-' . Str::random(8);

        try {
            $response = Http::timeout(30)->post("{$this->aiEngineUrl}/vectorize", [
                'doc_id'          => $docId,
                'title'           => $article->title,
                'content'         => $article->content,
                'regulation_type' => $article->regulation_type,
                'region'          => $article->region,
                'kbli_codes'      => $article->kbli_codes ?? [],
                'tags'            => $article->tags ?? [],
                'article_id'      => $article->id,
            ]);

            if ($response->successful()) {
                $article->updateQuietly([
                    'is_vectorized'   => true,
                    'vectorized_at'   => now(),
                    'chroma_doc_id'   => $docId,
                    'vectorize_error' => null,
                ]);
                Log::info('VectorizeService: article synced', ['id' => $article->id, 'doc_id' => $docId]);
                return ['success' => true, 'doc_id' => $docId, 'error' => null];
            }

            $error = "HTTP {$response->status()}: {$response->body()}";
            $article->updateQuietly(['vectorize_error' => $error]);
            Log::warning('VectorizeService: sync failed', ['id' => $article->id, 'status' => $response->status()]);
            return ['success' => false, 'doc_id' => null, 'error' => $error];

        } catch (\Throwable $e) {
            $article->updateQuietly(['vectorize_error' => $e->getMessage()]);
            Log::error('VectorizeService: exception', ['id' => $article->id, 'error' => $e->getMessage()]);
            return ['success' => false, 'doc_id' => null, 'error' => $e->getMessage()];
        }
    }
}
