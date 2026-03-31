<?php

use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Exceptions;
use Illuminate\Foundation\Configuration\Middleware;

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        web: __DIR__.'/../routes/web.php',
        api: __DIR__.'/../routes/api.php',
        commands: __DIR__.'/../routes/console.php',
        health: '/up',
    )
    ->withMiddleware(function (Middleware $middleware): void {
        $middleware->statefulApi();

        // Allow CORS for the Next.js frontend on API routes.
        $middleware->api(prepend: [
            \Illuminate\Http\Middleware\HandleCors::class,
        ]);
    })
    ->withExceptions(function (Exceptions $exceptions): void {
        // Render all API exceptions as clean JSON — never expose stack traces.
        $exceptions->render(function (\Throwable $e, \Illuminate\Http\Request $request) {
            if (! $request->is('api/*')) {
                return null; // Let the default HTML handler deal with web routes.
            }

            $status = match (true) {
                $e instanceof \Illuminate\Auth\AuthenticationException          => 401,
                $e instanceof \Illuminate\Auth\Access\AuthorizationException    => 403,
                $e instanceof \Illuminate\Database\Eloquent\ModelNotFoundException,
                $e instanceof \Symfony\Component\HttpKernel\Exception\NotFoundHttpException => 404,
                $e instanceof \Illuminate\Validation\ValidationException        => 422,
                $e instanceof \Symfony\Component\HttpKernel\Exception\HttpException => $e->getStatusCode(),
                default => 500,
            };

            $message = match (true) {
                $e instanceof \Illuminate\Auth\AuthenticationException          => 'Unauthenticated. Provide a valid Bearer token.',
                $e instanceof \Illuminate\Auth\Access\AuthorizationException    => 'You do not have permission to perform this action.',
                $e instanceof \Illuminate\Database\Eloquent\ModelNotFoundException,
                $e instanceof \Symfony\Component\HttpKernel\Exception\NotFoundHttpException => 'The requested resource was not found.',
                $e instanceof \Illuminate\Validation\ValidationException        => $e->getMessage(),
                $status >= 500                                                  => 'An unexpected error occurred. Please try again later.',
                default                                                         => $e->getMessage(),
            };

            if ($status >= 500) {
                \Illuminate\Support\Facades\Log::error('Unhandled API exception', [
                    'message'   => $e->getMessage(),
                    'exception' => get_class($e),
                    'file'      => $e->getFile(),
                    'line'      => $e->getLine(),
                ]);
            }

            return response()->json([
                'success' => false,
                'message' => $message,
            ], $status);
        });
    })->create();
