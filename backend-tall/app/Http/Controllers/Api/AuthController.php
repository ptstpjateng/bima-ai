<?php

namespace App\Http\Controllers\Api;

use App\Models\MagicLinkToken;
use App\Models\User;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Hash;
use Illuminate\Support\Str;
use Illuminate\Validation\ValidationException;

class AuthController extends ApiController
{
    /**
     * POST /api/auth/login
     * Email + password → Sanctum token.
     */
    public function login(Request $request): JsonResponse
    {
        try {
            $validated = $request->validate([
                'email'    => ['required', 'email'],
                'password' => ['required', 'string'],
            ]);

            $user = User::where('email', $validated['email'])->first();

            if (! $user || ! Hash::check($validated['password'], $user->password)) {
                return $this->error('Email atau password salah.', 401, 'INVALID_CREDENTIALS');
            }

            $token = $user->createToken('web-login', ['*'], now()->addDays(30))->plainTextToken;

            return $this->success([
                'token' => $token,
                'user'  => $this->userPayload($user),
            ], 200, 'Login berhasil.');

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'AuthController@login');
        }
    }

    /**
     * POST /api/auth/logout
     * Revoke the current token.
     */
    public function logout(Request $request): JsonResponse
    {
        try {
            $request->user()->currentAccessToken()->delete();

            return $this->success(null, 200, 'Logout berhasil.');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'AuthController@logout');
        }
    }

    /**
     * POST /api/auth/magic-link/generate
     * Generate a magic link token for a given user.
     * Intended for internal use by the AI engine via X-Internal-Key header.
     */
    public function generateMagicLink(Request $request): JsonResponse
    {
        try {
            // Allow: admin Sanctum users OR the AI engine with the internal key.
            $internalKey = config('app.internal_api_key');
            $providedKey  = $request->header('X-Internal-Key');

            $isInternal = $internalKey && $providedKey && hash_equals($internalKey, (string) $providedKey);

            // Resolve Sanctum user manually when not going through the middleware.
            $sanctumUser = $request->user('sanctum');
            $isAdmin = $sanctumUser && in_array($sanctumUser->role, ['admin', 'dpmptsp_staff']);

            if (! $isInternal && ! $isAdmin) {
                return $this->error('Akses ditolak.', 403, 'FORBIDDEN');
            }

            $validated = $request->validate([
                'user_id' => ['required', 'integer', 'exists:users,id'],
                'channel' => ['nullable', 'string', 'in:whatsapp,telegram,web,mobile'],
            ]);

            $rawToken = Str::random(48);

            $magic = MagicLinkToken::create([
                'user_id'    => $validated['user_id'],
                'token'      => $rawToken,
                'channel'    => $validated['channel'] ?? 'whatsapp',
                'expires_at' => now()->addMinutes(30),
            ]);

            // Magic link points to the backend web route (plain HTTP navigation —
            // not subject to mixed-content rules). Laravel redeems the token and
            // redirects the browser to the Vercel frontend with a Sanctum token.
            $appUrl   = config('app.url', 'http://116.254.113.81');
            $magicUrl = "{$appUrl}/magic/{$rawToken}";

            return $this->success([
                'magic_link' => $magicUrl,
                'expires_at' => $magic->expires_at->toIso8601String(),
            ], 201, 'Magic link dibuat.');

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'AuthController@generateMagicLink');
        }
    }

    /**
     * POST /api/auth/telegram/identify
     * Find or create a user by Telegram chat_id, then return a magic link.
     * Called exclusively by the AI engine via X-Internal-Key.
     */
    public function telegramIdentify(Request $request): JsonResponse
    {
        try {
            $internalKey = config('app.internal_api_key');
            $providedKey  = $request->header('X-Internal-Key');

            if (! $internalKey || ! $providedKey || ! hash_equals($internalKey, (string) $providedKey)) {
                return $this->error('Akses ditolak.', 403, 'FORBIDDEN');
            }

            $validated = $request->validate([
                'telegram_chat_id' => ['required', 'integer'],
                'first_name'       => ['nullable', 'string', 'max:100'],
                'last_name'        => ['nullable', 'string', 'max:100'],
                'username'         => ['nullable', 'string', 'max:100'],
            ]);

            $chatId = $validated['telegram_chat_id'];

            $user = User::where('telegram_chat_id', $chatId)->first();
            $isNew = false;

            if (! $user) {
                $firstName = $validated['first_name'] ?? 'Pengguna';
                $lastName  = $validated['last_name'] ?? '';
                $fullName  = trim("{$firstName} {$lastName}") ?: 'Pengguna Telegram';

                $user = User::create([
                    'name'               => $fullName,
                    'email'              => "tg_{$chatId}@bima-ai.local",
                    'password'           => Str::random(32),
                    'role'               => 'msme',
                    'telegram_chat_id'   => $chatId,
                    'telegram_username'  => $validated['username'] ?? null,
                ]);
                $isNew = true;
            } else {
                // Keep username in sync.
                if (isset($validated['username']) && $user->telegram_username !== $validated['username']) {
                    $user->update(['telegram_username' => $validated['username']]);
                }
            }

            $rawToken = Str::random(48);

            $magic = MagicLinkToken::create([
                'user_id'    => $user->id,
                'token'      => $rawToken,
                'channel'    => 'telegram',
                'expires_at' => now()->addMinutes(30),
            ]);

            // Magic link points to the backend web route (plain HTTP navigation —
            // not subject to mixed-content rules). Laravel redeems the token and
            // redirects the browser to the Vercel frontend with a Sanctum token.
            $appUrl   = config('app.url', 'http://116.254.113.81');
            $magicUrl = "{$appUrl}/magic/{$rawToken}";

            return $this->success([
                'magic_link'  => $magicUrl,
                'expires_at'  => $magic->expires_at->toIso8601String(),
                'user_id'     => $user->id,
                'user_name'   => $user->name,
                'is_new_user' => $isNew,
            ], 201, $isNew ? 'Akun baru dibuat.' : 'Magic link dibuat.');

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'AuthController@telegramIdentify');
        }
    }

    /**
     * GET /api/auth/magic/{token}
     * Exchange a magic link token for a Sanctum token + user data.
     */
    public function redeemMagicLink(string $rawToken): JsonResponse
    {
        try {
            $magic = MagicLinkToken::where('token', $rawToken)
                ->with('user')
                ->first();

            if (! $magic || ! $magic->isValid()) {
                return $this->error('Link tidak valid atau sudah kadaluarsa.', 401, 'INVALID_MAGIC_LINK');
            }

            $magic->update(['used_at' => now()]);

            $token = $magic->user->createToken(
                'magic-link-' . $magic->channel,
                ['*'],
                now()->addDays(7)
            )->plainTextToken;

            return $this->success([
                'token' => $token,
                'user'  => $this->userPayload($magic->user),
            ], 200, 'Autentikasi berhasil.');

        } catch (\Throwable $e) {
            return $this->serverError($e, 'AuthController@redeemMagicLink');
        }
    }

    /**
     * GET /api/auth/me
     * Return the current authenticated user's data.
     */
    public function me(Request $request): JsonResponse
    {
        return $this->success(['user' => $this->userPayload($request->user())]);
    }

    /**
     * Build a safe user payload to return to the frontend.
     */
    private function userPayload(User $user): array
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
