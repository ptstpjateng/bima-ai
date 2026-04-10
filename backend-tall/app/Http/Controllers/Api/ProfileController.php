<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use App\Models\MagicLinkToken;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Str;

class ProfileController extends Controller
{
    /**
     * PATCH /api/profile
     * Update editable MSME profile fields for the authenticated user.
     */
    public function update(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'name'             => ['sometimes', 'string', 'min:2', 'max:100'],
            'phone'            => ['sometimes', 'nullable', 'string', 'max:20'],
            'nik'              => ['sometimes', 'nullable', 'string', 'size:16'],
            'npwp'             => ['sometimes', 'nullable', 'string', 'max:20'],
            'business_name'    => ['sometimes', 'nullable', 'string', 'max:150'],
            'business_address' => ['sometimes', 'nullable', 'string', 'max:255'],
        ]);

        $user = $request->user();
        $user->update($validated);

        return response()->json([
            'success' => true,
            'message' => 'Profil berhasil diperbarui.',
            'data'    => ['user' => $this->userPayload($user->fresh())],
        ]);
    }

    /**
     * POST /api/profile/telegram-token
     * Generate a short-lived token for Telegram account linking.
     * The bot handles /start tglink_{token} by calling the internal link endpoint.
     */
    public function generateTelegramToken(Request $request): JsonResponse
    {
        $user = $request->user();

        // Invalidate any previous unused telegram link tokens for this user.
        MagicLinkToken::where('user_id', $user->id)
            ->where('channel', 'telegram_link')
            ->whereNull('used_at')
            ->delete();

        $token = Str::random(32);

        MagicLinkToken::create([
            'user_id'    => $user->id,
            'token'      => $token,
            'channel'    => 'telegram_link',
            'expires_at' => now()->addMinutes(15),
        ]);

        return response()->json([
            'success' => true,
            'message' => 'Token berhasil dibuat.',
            'data'    => ['token' => $token],
        ]);
    }

    private function userPayload(\App\Models\User $user): array
    {
        return [
            'id'                  => $user->id,
            'name'                => $user->name,
            'email'               => $user->email,
            'role'                => $user->role,
            'phone'               => $user->phone,
            'nik'                 => $user->nik,
            'npwp'                => $user->npwp,
            'business_name'       => $user->business_name,
            'business_legal_type' => $user->business_legal_type,
            'business_address'    => $user->business_address,
            'province'            => $user->province,
            'city'                => $user->city,
            'district'            => $user->district,
            'village'             => $user->village,
            'postal_code'         => $user->postal_code,
            'email_verified_at'   => $user->email_verified_at?->toIso8601String(),
            'telegram_chat_id'    => $user->telegram_chat_id,
            'telegram_username'   => $user->telegram_username,
        ];
    }
}
