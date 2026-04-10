<?php

use App\Http\Controllers\Api\AiLogController;
use App\Http\Controllers\Api\AuthController;
use App\Http\Controllers\Api\PdfJobController;
use App\Http\Controllers\Api\PermitController;
use App\Http\Controllers\Api\PipelineController;
use App\Http\Controllers\Api\ProfileController;
use App\Http\Controllers\Api\UserContextController;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;

// ── Health check (unauthenticated) ───────────────────────────────────────────
Route::get('/health', fn () => response()->json(['status' => 'ok', 'service' => 'BIMA-AI API']));

// ── Public auth routes ────────────────────────────────────────────────────────
Route::prefix('auth')->group(function () {
    Route::post('/login', [AuthController::class, 'login']);
    Route::get('/magic/{token}', [AuthController::class, 'redeemMagicLink']);

    // Magic link generation via X-Internal-Key (AI engine / internal services).
    Route::post('/magic-link/generate', [AuthController::class, 'generateMagicLink']);

    // Telegram: find-or-create user by chat_id and return a magic link.
    Route::post('/telegram/identify', [AuthController::class, 'telegramIdentify']);
});

// ── Internal AI-engine endpoints (X-Internal-Key) ──────────────────────────
Route::prefix('internal')->group(function () {
    Route::get('/user-context/{userId}', [UserContextController::class, 'show']);
    Route::post('/ai-logs', [AiLogController::class, 'storeInternal']);

    // Data pipeline: queue management and status reporting
    Route::get('/pipeline/queue', [PipelineController::class, 'queue']);
    Route::post('/pipeline/status', [PipelineController::class, 'updateStatus']);

    // Telegram account linking: called by the bot after /start tglink_{token}
    Route::post('/telegram/link', [AuthController::class, 'telegramLink']);

    // PDF parsing jobs: lifecycle callbacks from pdf_agent_pipeline.py
    Route::prefix('pdf-jobs')->group(function () {
        Route::get('/{id}', [PdfJobController::class, 'show'])->whereNumber('id');
        Route::patch('/{id}/progress', [PdfJobController::class, 'updateProgress'])->whereNumber('id');
        Route::post('/{id}/complete', [PdfJobController::class, 'complete'])->whereNumber('id');
        Route::post('/{id}/fail', [PdfJobController::class, 'fail'])->whereNumber('id');
    });
});

// ── Magic link generation via Sanctum (admin/staff via dashboard) ─────────────
Route::middleware('auth:sanctum')->prefix('auth')->group(function () {
    Route::post('/magic-link/generate-admin', [AuthController::class, 'generateMagicLink']);
});

// ── Authenticated routes (Sanctum token) ─────────────────────────────────────
Route::middleware(['auth:sanctum', 'throttle:120,1'])->group(function () {

    Route::get('/user', fn (Request $request) => response()->json($request->user()));
    Route::get('/auth/me', [AuthController::class, 'me']);
    Route::post('/auth/logout', [AuthController::class, 'logout']);

    // AI Engine: push chat log entries + fetch history for current user
    Route::post('/ai-logs', [AiLogController::class, 'store']);
    Route::get('/ai-logs', [AiLogController::class, 'index']);

    // Profile management
    Route::patch('/profile', [ProfileController::class, 'update']);
    Route::post('/profile/telegram-token', [ProfileController::class, 'generateTelegramToken']);

    // Permit Applications
    Route::get('/permits/{user_id}', [PermitController::class, 'index'])
        ->whereNumber('user_id');
    Route::get('/permits/detail/{id}', [PermitController::class, 'show'])
        ->whereNumber('id');
    Route::post('/permits/apply', [PermitController::class, 'apply']);

    // KBLI search — returns scraped KBLI codes with descriptions for autocomplete.
    // ?q= filters by code or description (min 2 chars), returns up to 10 results.
    Route::get('/kbli', function (Request $request) {
        $q = trim((string) $request->query('q', ''));
        $query = \App\Models\KbliScrapeTarget::where('status', 'done')
            ->orderBy('kbli_code');
        if (mb_strlen($q) >= 2) {
            $query->where(function ($sub) use ($q) {
                $sub->where('kbli_code', 'like', "%{$q}%")
                    ->orWhere('kbli_description', 'like', "%{$q}%");
            });
        }
        $items = $query->limit(10)->get(['kbli_code', 'kbli_description', 'sector']);
        return response()->json(['success' => true, 'data' => $items]);
    });
});
