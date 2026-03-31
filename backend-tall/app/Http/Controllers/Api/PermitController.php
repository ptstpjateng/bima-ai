<?php

namespace App\Http\Controllers\Api;

use App\Models\PermitApplication;
use App\Models\User;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Validation\ValidationException;

class PermitController extends ApiController
{
    /**
     * GET /api/permits/{user_id}
     *
     * Returns active permit applications for a given user.
     * The authenticated token owner may only fetch their own permits,
     * unless they hold an admin or staff role.
     */
    public function index(Request $request, int $userId): JsonResponse
    {
        try {
            /** @var \App\Models\User $caller */
            $caller = $request->user();

            // Non-admin callers may only query their own permits.
            if ($caller->role === 'msme' && $caller->id !== $userId) {
                return $this->error('You are not authorized to view these permits.', 403, 'FORBIDDEN');
            }

            $user = User::find($userId);

            if (! $user) {
                return $this->error('User not found.', 404, 'USER_NOT_FOUND');
            }

            $permits = PermitApplication::where('user_id', $userId)
                ->whereNotIn('status', ['expired', 'rejected'])
                ->orderByDesc('created_at')
                ->get([
                    'id',
                    'application_number',
                    'oss_application_number',
                    'nib',
                    'kbli_code',
                    'kbli_description',
                    'business_scale',
                    'risk_level',
                    'status',
                    'submitted_at',
                    'approved_at',
                    'permit_expiry_date',
                    'created_at',
                ]);

            return $this->success([
                'user_id' => $userId,
                'permits' => $permits,
                'total'   => $permits->count(),
            ]);

        } catch (\Throwable $e) {
            return $this->serverError($e, 'PermitController@index');
        }
    }

    /**
     * POST /api/permits/apply
     *
     * Submits a new permit application from the Next.js / React Native frontend
     * after the AI guidance phase is complete.
     */
    public function apply(Request $request): JsonResponse
    {
        try {
            $validated = $request->validate([
                'kbli_code'                  => ['required', 'string', 'max:10'],
                'kbli_description'           => ['required', 'string', 'max:255'],
                'kbli_section'               => ['nullable', 'string', 'max:100'],
                'business_scale'             => ['required', 'string', 'in:mikro,kecil,menengah,besar'],
                'risk_level'                 => ['required', 'string', 'in:rendah,menengah_rendah,menengah_tinggi,tinggi'],
                'business_location_address'  => ['nullable', 'string'],
                'business_location_province' => ['nullable', 'string', 'max:100'],
                'business_location_city'     => ['nullable', 'string', 'max:100'],
                'annual_revenue_estimate'    => ['nullable', 'integer', 'min:0'],
                'employee_count'             => ['nullable', 'integer', 'min:0'],
                'applicant_notes'            => ['nullable', 'string'],
                'documents'                  => ['nullable', 'array'],
                'documents.*.name'           => ['required_with:documents', 'string', 'max:255'],
                'documents.*.path'           => ['required_with:documents', 'string', 'max:500'],
                'documents.*.type'           => ['required_with:documents', 'string', 'max:100'],
            ]);

            /** @var \App\Models\User $user */
            $user = $request->user();

            $permit = DB::transaction(function () use ($validated, $user) {
                $applicationNumber = $this->generateApplicationNumber();

                return PermitApplication::create([
                    ...$validated,
                    'user_id'            => $user->id,
                    'application_number' => $applicationNumber,
                    'status'             => 'submitted',
                    'submitted_at'       => now(),
                ]);
            });

            return $this->success(
                [
                    'id'                 => $permit->id,
                    'application_number' => $permit->application_number,
                    'status'             => $permit->status,
                    'submitted_at'       => $permit->submitted_at,
                ],
                201,
                'Permit application submitted successfully.'
            );

        } catch (ValidationException $e) {
            return $this->error($e->getMessage(), 422, 'VALIDATION_ERROR');
        } catch (\Throwable $e) {
            return $this->serverError($e, 'PermitController@apply');
        }
    }

    /**
     * Generate a sequential BIMA-AI application reference number.
     * Format: BIMA-YYYY-NNNNN
     */
    private function generateApplicationNumber(): string
    {
        $year  = now()->format('Y');
        $prefix = "BIMA-{$year}-";

        $last = PermitApplication::where('application_number', 'like', "{$prefix}%")
            ->orderByDesc('application_number')
            ->value('application_number');

        $sequence = $last
            ? (int) substr($last, strlen($prefix)) + 1
            : 1;

        return $prefix . str_pad($sequence, 5, '0', STR_PAD_LEFT);
    }
}
