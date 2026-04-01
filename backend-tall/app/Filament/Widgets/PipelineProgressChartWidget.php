<?php

namespace App\Filament\Widgets;

use App\Models\KbliScrapeTarget;
use Filament\Widgets\ChartWidget;

class PipelineProgressChartWidget extends ChartWidget
{
    protected static ?int $sort = 2;

    protected ?string $heading = 'Progress Pipeline KBLI';

    protected ?string $description = 'Distribusi status scraping 35 kode KBLI';

    protected function getData(): array
    {
        $done     = KbliScrapeTarget::where('status', 'done')->count();
        $pending  = KbliScrapeTarget::where('status', 'pending')->count();
        $scraping = KbliScrapeTarget::whereIn('status', ['queued', 'scraping'])->count();
        $failed   = KbliScrapeTarget::where('status', 'failed')->count();

        return [
            'datasets' => [
                [
                    'label'           => 'KBLI',
                    'data'            => [$done, $scraping, $pending, $failed],
                    'backgroundColor' => [
                        'rgba(34, 197, 94, 0.8)',   // green  – done
                        'rgba(234, 179, 8, 0.8)',   // yellow – scraping/queued
                        'rgba(148, 163, 184, 0.8)', // gray   – pending
                        'rgba(239, 68, 68, 0.8)',   // red    – failed
                    ],
                    'borderColor' => [
                        'rgb(22, 163, 74)',
                        'rgb(202, 138, 4)',
                        'rgb(100, 116, 139)',
                        'rgb(220, 38, 38)',
                    ],
                    'borderWidth' => 2,
                    'hoverOffset' => 6,
                ],
            ],
            'labels' => [
                "Selesai ({$done})",
                "Diproses ({$scraping})",
                "Menunggu ({$pending})",
                "Gagal ({$failed})",
            ],
        ];
    }

    protected function getType(): string
    {
        return 'doughnut';
    }

    protected function getOptions(): array
    {
        return [
            'plugins' => [
                'legend' => [
                    'position' => 'bottom',
                    'labels'   => ['padding' => 20, 'usePointStyle' => true],
                ],
            ],
            'cutout'       => '65%',
            'maintainAspectRatio' => false,
        ];
    }
}
