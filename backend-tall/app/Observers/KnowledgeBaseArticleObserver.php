<?php

namespace App\Observers;

use App\Models\KnowledgeBaseArticle;
use App\Services\VectorizeService;

class KnowledgeBaseArticleObserver
{
    /**
     * After create/update: if the article is published and content has changed
     * (or has never been vectorized), trigger async re-vectorization.
     */
    public function saved(KnowledgeBaseArticle $article): void
    {
        if (! $article->published_at) {
            return;
        }

        // Re-vectorize when: never vectorized OR content/title changed on update
        $contentChanged = $article->wasChanged(['content', 'title', 'kbli_codes', 'tags', 'region', 'regulation_type']);
        if (! $article->is_vectorized || $contentChanged) {
            dispatch(function () use ($article) {
                (new VectorizeService())->sync($article);
            })->afterResponse();
        }
    }
}
