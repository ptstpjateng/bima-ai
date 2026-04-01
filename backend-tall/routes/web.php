<?php

use App\Models\MagicLinkToken;
use Illuminate\Support\Facades\Route;

/*
 * Browser-facing magic link redemption.
 *
 * The Telegram bot sends links pointing here (plain HTTP — browser navigation
 * is not subject to mixed-content rules). Laravel redeems the one-time token
 * and redirects the browser to the Vercel frontend with a short-lived Sanctum
 * token so the frontend never needs to make a direct HTTP API call.
 *
 * URL: http://116.254.113.81/magic/{token}
 * Redirects to: https://project-5z22k.vercel.app/auth/callback?api_token=...
 */
Route::get('/magic/{token}', function (string $rawToken) {
    $magic = MagicLinkToken::where('token', $rawToken)->with('user')->first();

    $frontendUrl = config('app.frontend_url', 'https://project-5z22k.vercel.app');

    if (! $magic || ! $magic->isValid()) {
        return redirect($frontendUrl . '/auth/magic?error=invalid_or_expired');
    }

    $magic->update(['used_at' => now()]);

    $sanctumToken = $magic->user->createToken(
        'magic-link-' . $magic->channel,
        ['*'],
        now()->addDays(7)
    )->plainTextToken;

    $user = $magic->user;
    $query = http_build_query([
        'api_token' => $sanctumToken,
        'name'      => $user->name,
        'email'     => $user->email,
        'role'      => $user->role,
        'user_id'   => $user->id,
    ]);

    return redirect($frontendUrl . '/auth/callback?' . $query);
});
