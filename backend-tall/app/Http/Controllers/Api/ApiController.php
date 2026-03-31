<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\Log;
use Throwable;

abstract class ApiController extends Controller
{
    /**
     * Return a standardized success JSON response.
     */
    protected function success(mixed $data, int $status = 200, string $message = 'OK'): JsonResponse
    {
        return response()->json([
            'success' => true,
            'message' => $message,
            'data'    => $data,
        ], $status);
    }

    /**
     * Return a standardized error JSON response.
     * Never leaks exception details to the client.
     */
    protected function error(string $message, int $status = 400, ?string $errorCode = null): JsonResponse
    {
        $body = [
            'success' => false,
            'message' => $message,
        ];

        if ($errorCode !== null) {
            $body['error_code'] = $errorCode;
        }

        return response()->json($body, $status);
    }

    /**
     * Log an exception securely and return a generic 500 response.
     */
    protected function serverError(Throwable $e, string $context = ''): JsonResponse
    {
        Log::error('API error' . ($context ? " [{$context}]" : ''), [
            'message'   => $e->getMessage(),
            'exception' => get_class($e),
            'file'      => $e->getFile(),
            'line'      => $e->getLine(),
            'trace'     => $e->getTraceAsString(),
        ]);

        return $this->error('An unexpected error occurred. Please try again later.', 500, 'INTERNAL_SERVER_ERROR');
    }
}
