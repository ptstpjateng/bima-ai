<?php

namespace App\Filament\Widgets;

use App\Models\KbliScrapeTarget;
use App\Models\KnowledgeBaseArticle;
use Filament\Widgets\StatsOverviewWidget;
use Filament\Widgets\StatsOverviewWidget\Stat;

class PipelineStatsWidget extends StatsOverviewWidget
{
    protected static ?int $sort = -2;

    protected int|string $columnSpan = 'full';

    protected function getStats(): array
    {
        $totalChunks    = (int) KbliScrapeTarget::sum('chroma_chunks');
        $totalTargets   = KbliScrapeTarget::count();
        $doneTargets    = KbliScrapeTarget::where('status', 'done')->count();
        $pendingTargets = KbliScrapeTarget::whereIn('status', ['pending', 'queued', 'scraping'])->count();
        $failedTargets  = KbliScrapeTarget::where('status', 'failed')->count();
        $articles       = KnowledgeBaseArticle::whereNotNull('published_at')->count();

        return [
            Stat::make('Chunk di ChromaDB', number_format($totalChunks))
                ->description('Total potongan regulasi terindeks untuk RAG')
                ->descriptionIcon('heroicon-o-circle-stack')
                ->color('success'),

            Stat::make('Pipeline KBLI', "{$doneTargets} / {$totalTargets} selesai")
                ->description(
                    match(true) {
                        $pendingTargets > 0 && $failedTargets > 0 => "{$pendingTargets} menunggu · {$failedTargets} gagal",
                        $pendingTargets > 0                        => "{$pendingTargets} menunggu diproses",
                        $failedTargets > 0                         => "{$failedTargets} kode gagal",
                        default                                    => 'Semua target selesai',
                    }
                )
                ->descriptionIcon(match(true) {
                    $failedTargets > 0   => 'heroicon-o-exclamation-circle',
                    $pendingTargets > 0  => 'heroicon-o-clock',
                    default              => 'heroicon-o-check-circle',
                })
                ->color(match(true) {
                    $failedTargets > 0  => 'danger',
                    $pendingTargets > 0 => 'warning',
                    default             => 'success',
                }),

            Stat::make('Artikel Regulasi', (string) $articles)
                ->description('Regulasi & panduan terpublikasi')
                ->descriptionIcon('heroicon-o-book-open')
                ->color('info'),
        ];
    }
}
