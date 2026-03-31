<?php

namespace App\Http\Controllers\Api;

use App\Models\AiInteraction;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Str;
use Illuminate\Validation\ValidationException;

class AiLogController extends ApiController
{
    /**
     * GET /api/ai-logs
     *
     * Returns the AI interaction history for the authenticated user.
     * Returns the 50 most recent interactions across all sessions.
     */
    public function index(Request $request): JsonResponse
    {
        try {
            /** @var \App\Models\User $user */
            $user = $request->user();

            $interactions = AiInteraction::where('user_id', $user->id)
                ->orderByDesc('created_at')
                ->limit(50)
                ->get([
                    'id',
                    'session_id',
                    'turn_index',
                    'channel',
                    'message_type',
                    'content',
                    'intent',
                    'confidence_score',
                    'created_at',
                ]);

            return $this->success([
                'interactions' => $interactions,
                'total'        => $interactions->count(),
            ]);

        } catch (\Throwable $e) {
            return $this->serverError($e, 'AiLogController@index');
        }
    }

    /**
     * POST /api/internal/ai-logs
     *
     * Called by the Python AI engine via X-Internal-Key header.
     * No Sanctum token required.
     */
    public function storeInternal(Request $request): JsonResponse
    {
        $internalKey = config('app.internal_api_key');
        $providedKey  = $request->header('X-Internal-Key');

        if (! $internalKey || ! $providedKey || ! hash_equals($internalKey, (string) $providedKey)) {
            return $this->error('Akses ditolak.', 403, 'FORBIDDEN');
        }

        return $this->store($request);
    }

    /**
     * POST /api/ai-logs
     *
     * Called by the Python AI engine to push a chat log entry.
     * Protected by Sanctum token auth.
     */
    public function store(Request $request): JsonResponse
    {
        try {
            $validated = $request->validate([
                'session_id'             => ['required', 'string', 'max:64'],
                'turn_index'             => ['required', 'integer', 'min:0'],
                'channel'                => ['required', 'string', 'in:whatsapp,telegram,web,mobile,internal'],
                'message_type'           => ['required', 'string', 'in:user_message,ai_response,system_event'],
                'content'                => ['required', 'string'],
                'content_rendered'       => ['nullable', 'string'],
                'intent'                 => ['nullable', 'string', 'max:100'],
                'entities'               => ['nullable', 'array'],
                'context_snapshot'       => ['nullable', 'array'],
                'model_used'             => ['nullable', 'string', 'max:80'],
                'prompt_tokens'          => ['nullable', 'integer', 'min:0'],
                'completion_tokens'      => ['nullable', 'integer', 'min:0'],
                'response_time_ms'       => ['nullable', 'integer', 'min:0'],
                'confidence_score'       => ['nullable', 'numeric', 'min:0', 'max:1'],
                'external_message_id'    => ['nullable', 'string', 'max:100'],
                'user_id'                => ['nullable', 'integer', 'exists:users,id'],
                'permit_application_id'  => ['nullable', 'integer', 'exists:permit_applications,id'],
                'is_sensitive'           => ['nullable', 'boolean'],
            ]);

            $interaction = AiInteraction::create($validated);

            return $this->success(
                ['id' => $interaction->id],
                201,
                'Interaction logged.'
            );

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'AiLogController@store');
        }
    }
}
