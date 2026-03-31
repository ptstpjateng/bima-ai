<?php

namespace App\Http\Controllers\Api;

use App\Models\PermitApplication;
use App\Models\User;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;

class UserContextController extends ApiController
{
    /**
     * GET /api/internal/user-context/{userId}
     * Returns a structured context snapshot for the AI engine to personalize responses.
     * Protected by X-Internal-Key.
     */
    public function show(Request $request, int $userId): JsonResponse
    {
        $internalKey = config('app.internal_api_key');
        $providedKey = $request->header('X-Internal-Key');

        if (! $internalKey || ! $providedKey || ! hash_equals($internalKey, (string) $providedKey)) {
            return $this->error('Akses ditolak.', 403, 'FORBIDDEN');
        }

        try {
            $user = User::with(['businesses' => fn ($q) => $q->where('is_primary', true)->limit(1)])
                ->find($userId);

            if (! $user) {
                return $this->success([
                    'found'   => false,
                    'user_id' => $userId,
                ]);
            }

            $activeLicenses = PermitApplication::where('user_id', $userId)
                ->whereIn('status', ['approved'])
                ->whereNotNull('nib')
                ->latest('approved_at')
                ->limit(5)
                ->get(['id', 'kbli_code', 'kbli_description', 'business_scale', 'risk_level', 'status', 'nib', 'approved_at', 'permit_expiry_date']);

            $pendingApplications = PermitApplication::where('user_id', $userId)
                ->whereIn('status', ['submitted', 'under_review', 'additional_docs_required'])
                ->latest('submitted_at')
                ->limit(3)
                ->get(['id', 'kbli_code', 'kbli_description', 'business_scale', 'risk_level', 'status', 'submitted_at']);

            $primaryBusiness = $user->businesses->first();

            return $this->success([
                'found'    => true,
                'user'     => [
                    'id'             => $user->id,
                    'name'           => $user->name,
                    'business_name'  => $primaryBusiness?->name ?? $user->business_name,
                    'business_scale' => $primaryBusiness?->scale,
                    'legal_type'     => $primaryBusiness?->legal_type ?? $user->business_legal_type,
                    'city'           => $primaryBusiness?->city ?? $user->city,
                    'province'       => $primaryBusiness?->province ?? $user->province,
                    'kbli_code'      => $primaryBusiness?->primary_kbli_code,
                    'kbli_description' => $primaryBusiness?->primary_kbli_description,
                    'employee_count' => $primaryBusiness?->employee_count,
                ],
                'active_licenses'      => $activeLicenses,
                'pending_applications' => $pendingApplications,
            ]);

        } catch (\Throwable $e) {
            return $this->serverError($e, 'UserContextController@show');
        }
    }
}
