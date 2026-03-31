<?php

namespace App\Observers;

use App\Models\KnowledgeBaseArticle;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;

class KnowledgeBaseArticleObserver
{
    /**
     * After a create or update: if the article is published and not yet
     * vectorized, dispatch an async vectorization request to the AI engine.
     */
    public function saved(KnowledgeBaseArticle $article): void
    {
        if (! $article->published_at || $article->is_vectorized) {
            return;
        }

        dispatch(function () use ($article) {
            $this->vectorize($article);
        })->afterResponse();
    }

    private function vectorize(KnowledgeBaseArticle $article): void
    {
        $aiEngineUrl = config('services.ai_engine.url', env('AI_ENGINE_URL', 'http://ai-engine:8000'));

        try {
            $docId = $article->chroma_doc_id ?? 'kb-' . $article->id . '-' . Str::random(8);

            $response = Http::timeout(30)->post("{$aiEngineUrl}/vectorize", [
                'doc_id'           => $docId,
                'title'            => $article->title,
                'content'          => $article->content,
                'regulation_type'  => $article->regulation_type,
                'region'           => $article->region,
                'kbli_codes'       => $article->kbli_codes ?? [],
                'tags'             => $article->tags ?? [],
                'article_id'       => $article->id,
            ]);

            if ($response->successful()) {
                $article->updateQuietly([
                    'is_vectorized'   => true,
                    'vectorized_at'   => now(),
                    'chroma_doc_id'   => $docId,
                    'vectorize_error' => null,
                ]);
                Log::info("KnowledgeBase article vectorized", ['id' => $article->id, 'doc_id' => $docId]);
            } else {
                $article->updateQuietly([
                    'vectorize_error' => "HTTP {$response->status()}: {$response->body()}",
                ]);
                Log::warning("Vectorize failed", ['id' => $article->id, 'status' => $response->status()]);
            }
        } catch (\Throwable $e) {
            $article->updateQuietly(['vectorize_error' => $e->getMessage()]);
            Log::error("Vectorize exception", ['id' => $article->id, 'error' => $e->getMessage()]);
        }
    }
}
