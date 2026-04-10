<?php

namespace App\Observers;

use App\Models\PermitApplication;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;

class PermitApplicationObserver
{
    /**
     * Fire a Telegram notification when a permit status changes to a
     * user-facing milestone (approved, rejected, additional_docs_required).
     */
    public function updated(PermitApplication $permit): void
    {
        if (! $permit->wasChanged('status')) {
            return;
        }

        $newStatus = $permit->status;

        $notifiable = in_array($newStatus, [
            'approved',
            'rejected',
            'additional_docs_required',
            'under_review',
        ]);

        if (! $notifiable) {
            return;
        }

        $user = $permit->user;

        if (! $user || ! $user->telegram_chat_id) {
            return;
        }

        $message = $this->buildMessage($permit, $newStatus);

        $this->sendTelegram((int) $user->telegram_chat_id, $message);
    }

    private function buildMessage(PermitApplication $permit, string $status): string
    {
        $app  = $permit->application_number;
        $kbli = $permit->kbli_description;
        $portal = config('app.frontend_url', 'https://project-5z22k.vercel.app');

        return match ($status) {
            'approved' => implode("\n\n", [
                "✅ *Izin Usaha Disetujui!*",
                "Permohonan *{$app}* ({$kbli}) telah disetujui.",
                ($permit->nib ? "NIB Anda: `{$permit->nib}`" : ""),
                "Selamat! Pastikan Anda memenuhi kewajiban pelaporan LKPM setiap triwulan.",
                "[Lihat detail di portal]({$portal}/permits/{$permit->id})",
            ]),

            'rejected' => implode("\n\n", [
                "❌ *Permohonan Ditolak*",
                "Permohonan *{$app}* ({$kbli}) tidak dapat diproses.",
                ($permit->reviewer_notes ? "Alasan: {$permit->reviewer_notes}" : ""),
                "Silakan perbaiki dan ajukan permohonan baru.",
                "[Lihat detail di portal]({$portal}/permits/{$permit->id})",
            ]),

            'additional_docs_required' => implode("\n\n", [
                "📋 *Dokumen Tambahan Diperlukan*",
                "Permohonan *{$app}* ({$kbli}) memerlukan dokumen tambahan.",
                ($permit->reviewer_notes ? "Catatan petugas: {$permit->reviewer_notes}" : ""),
                "[Unggah dokumen di portal]({$portal}/permits/{$permit->id})",
            ]),

            'under_review' => implode("\n\n", [
                "🔍 *Permohonan Sedang Ditinjau*",
                "Permohonan *{$app}* ({$kbli}) sedang dalam proses peninjauan oleh petugas DPMPTSP.",
                "Kami akan memberi tahu Anda ketika ada pembaruan.",
            ]),

            default => '',
        };
    }

    private function sendTelegram(int $chatId, string $message): void
    {
        $message = trim($message);
        if (! $message) {
            return;
        }

        $token = config('services.telegram.bot_token');
        if (! $token) {
            Log::warning('PermitApplicationObserver: TELEGRAM_BOT_TOKEN not configured');
            return;
        }

        try {
            Http::timeout(8)->post("https://api.telegram.org/bot{$token}/sendMessage", [
                'chat_id'                  => $chatId,
                'text'                     => $message,
                'parse_mode'               => 'Markdown',
                'disable_web_page_preview' => true,
            ]);
        } catch (\Throwable $e) {
            Log::error('PermitApplicationObserver: Telegram send failed', [
                'chat_id' => $chatId,
                'error'   => $e->getMessage(),
            ]);
        }
    }
}
