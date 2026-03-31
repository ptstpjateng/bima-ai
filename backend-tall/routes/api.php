<?php

use App\Http\Controllers\Api\AiLogController;
use App\Http\Controllers\Api\AuthController;
use App\Http\Controllers\Api\PermitController;
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
});

// ── Magic link generation via Sanctum (admin/staff via dashboard) ─────────────
Route::middleware('auth:sanctum')->prefix('auth')->group(function () {
    Route::post('/magic-link/generate-admin', [AuthController::class, 'generateMagicLink']);
});

// ── Authenticated routes (Sanctum token) ─────────────────────────────────────
Route::middleware('auth:sanctum')->group(function () {

    Route::get('/user', fn (Request $request) => response()->json($request->user()));
    Route::get('/auth/me', [AuthController::class, 'me']);
    Route::post('/auth/logout', [AuthController::class, 'logout']);

    // AI Engine: push chat log entries + fetch history for current user
    Route::post('/ai-logs', [AiLogController::class, 'store']);
    Route::get('/ai-logs', [AiLogController::class, 'index']);

    // Permit Applications
    Route::get('/permits/{user_id}', [PermitController::class, 'index'])
        ->whereNumber('user_id');
    Route::post('/permits/apply', [PermitController::class, 'apply']);
});
