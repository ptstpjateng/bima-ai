<?php

namespace App\Filament\Widgets;

use App\Models\AiInteraction;
use App\Models\PermitApplication;
use App\Models\User;
use Filament\Widgets\StatsOverviewWidget;
use Filament\Widgets\StatsOverviewWidget\Stat;

class UserStatsWidget extends StatsOverviewWidget
{
    protected static ?int $sort = 2;

    protected array|string|int $columnSpan = 'full';

    protected function getStats(): array
    {
        $totalUsers    = User::where('role', 'msme')->count();
        $telegramLinked = User::where('role', 'msme')->whereNotNull('telegram_chat_id')->count();
        $activeUsers   = AiInteraction::query()
            ->where('created_at', '>=', now()->subHours(24))
            ->whereNotNull('user_id')
            ->distinct('user_id')
            ->count('user_id');
        $pendingPermits  = PermitApplication::whereIn('status', ['submitted', 'under_review'])->count();
        $approvedPermits = PermitApplication::where('status', 'approved')->count();
        $todayPermits    = PermitApplication::whereDate('created_at', today())->count();

        return [
            Stat::make('Pengguna UMKM', (string) $totalUsers)
                ->description("{$telegramLinked} terhubung Telegram")
                ->descriptionIcon('heroicon-m-users')
                ->color('primary'),

            Stat::make('Aktif (24 jam)', (string) $activeUsers)
                ->description('Pengguna unik yang berinteraksi dengan AI')
                ->descriptionIcon('heroicon-m-signal')
                ->color($activeUsers > 0 ? 'success' : 'gray'),

            Stat::make('Permohonan Menunggu', (string) $pendingPermits)
                ->description("{$approvedPermits} telah disetujui · {$todayPermits} masuk hari ini")
                ->descriptionIcon('heroicon-m-document-check')
                ->color($pendingPermits > 0 ? 'warning' : 'success'),
        ];
    }
}
