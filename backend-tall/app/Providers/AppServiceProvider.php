<?php

namespace App\Providers;

use App\Models\KnowledgeBaseArticle;
use App\Observers\KnowledgeBaseArticleObserver;
use Illuminate\Support\ServiceProvider;

class AppServiceProvider extends ServiceProvider
{
    public function register(): void {}

    public function boot(): void
    {
        KnowledgeBaseArticle::observe(KnowledgeBaseArticleObserver::class);
    }
}
