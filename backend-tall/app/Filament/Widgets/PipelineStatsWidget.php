<?php

namespace App\Filament\Widgets;

use App\Models\KbliScrapeTarget;
use App\Models\KnowledgeBaseArticle;
use App\Models\AiInteraction;
use Filament\Widgets\StatsOverviewWidget;
use Filament\Widgets\StatsOverviewWidget\Stat;

class PipelineStatsWidget extends StatsOverviewWidget
{
    protected static ?int $sort = 1;

    protected array|string|int $columnSpan = 'full';

    protected function getStats(): array
    {
        $totalChunks    = (int) KbliScrapeTarget::sum('chroma_chunks');
        $totalTargets   = KbliScrapeTarget::count();
        $doneTargets    = KbliScrapeTarget::where('status', 'done')->count();
        $scrapingNow    = KbliScrapeTarget::whereIn('status', ['queued', 'scraping'])->count();
        $pendingTargets = KbliScrapeTarget::where('status', 'pending')->count();
        $failedTargets  = KbliScrapeTarget::where('status', 'failed')->count();
        $articles       = KnowledgeBaseArticle::whereNotNull('published_at')->count();
        $todayChats     = AiInteraction::where('message_type', 'ai_response')
            ->whereDate('created_at', today())
            ->count();

        $progressPct = $totalTargets > 0 ? round(($doneTargets / $totalTargets) * 100) : 0;

        return [
            Stat::make('Chunk di ChromaDB', number_format($totalChunks))
                ->description('Potongan regulasi terindeks untuk RAG')
                ->descriptionIcon('heroicon-m-circle-stack')
                ->color('success')
                ->chart([$totalChunks > 0 ? max(1, $totalChunks - 20) : 0, $totalChunks]),

            Stat::make('Pipeline KBLI', "{$progressPct}% selesai")
                ->description(
                    match(true) {
                        $scrapingNow > 0  => "{$scrapingNow} sedang diproses · {$pendingTargets} antri · {$failedTargets} gagal",
                        $pendingTargets > 0 => "{$doneTargets}/{$totalTargets} done · {$pendingTargets} menunggu · {$failedTargets} gagal",
                        $failedTargets > 0  => "{$doneTargets}/{$totalTargets} done · {$failedTargets} gagal",
                        default             => "{$doneTargets}/{$totalTargets} KBLI berhasil diindeks",
                    }
                )
                ->descriptionIcon(match(true) {
                    $scrapingNow > 0   => 'heroicon-m-arrow-path',
                    $failedTargets > 0 => 'heroicon-m-exclamation-circle',
                    $pendingTargets > 0 => 'heroicon-m-clock',
                    default            => 'heroicon-m-check-circle',
                })
                ->color(match(true) {
                    $scrapingNow > 0   => 'warning',
                    $failedTargets > 0 => 'danger',
                    $pendingTargets > 0 => 'info',
                    default            => 'success',
                }),

            Stat::make('Artikel Regulasi', (string) $articles)
                ->description('Regulasi & panduan terpublikasi')
                ->descriptionIcon('heroicon-m-book-open')
                ->color('info'),

            Stat::make('Chat AI Hari Ini', (string) $todayChats)
                ->description('Respons AI yang dikirim hari ini')
                ->descriptionIcon('heroicon-m-chat-bubble-left-right')
                ->color('primary'),
        ];
    }
}
