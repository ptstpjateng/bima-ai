<?php

use App\Http\Controllers\Api\AiLogController;
use App\Http\Controllers\Api\PermitController;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;

/*
|--------------------------------------------------------------------------
| API Routes
|--------------------------------------------------------------------------
|
| All routes below are protected by Sanctum token authentication.
| Unauthenticated requests receive a standardized 401 JSON response.
|
*/

// Health check — unauthenticated, used by uptime monitors.
Route::get('/health', fn () => response()->json(['status' => 'ok', 'service' => 'BIMA-AI API']));

// ---------------------------------------------------------------------------
// Authenticated routes (Sanctum token)
// ---------------------------------------------------------------------------
Route::middleware('auth:sanctum')->group(function () {

    // Authenticated user info
    Route::get('/user', fn (Request $request) => response()->json($request->user()));

    // --- AI Engine -----------------------------------------------------------
    // POST /api/ai-logs
    // Python AI engine pushes chat log entries here.
    Route::post('/ai-logs', [AiLogController::class, 'store']);

    // --- Permit Applications -------------------------------------------------
    // GET  /api/permits/{user_id}  — fetch active permits for a user
    // POST /api/permits/apply      — submit a new permit application
    Route::get('/permits/{user_id}', [PermitController::class, 'index'])
        ->whereNumber('user_id');

    Route::post('/permits/apply', [PermitController::class, 'apply']);
});
