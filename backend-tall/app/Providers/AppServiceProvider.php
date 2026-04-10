<?php

namespace App\Providers;

use App\Models\KnowledgeBaseArticle;
use App\Models\PermitApplication;
use App\Observers\KnowledgeBaseArticleObserver;
use App\Observers\PermitApplicationObserver;
use Illuminate\Support\ServiceProvider;

class AppServiceProvider extends ServiceProvider
{
    public function register(): void {}

    public function boot(): void
    {
        KnowledgeBaseArticle::observe(KnowledgeBaseArticleObserver::class);
        PermitApplication::observe(PermitApplicationObserver::class);
    }
}
