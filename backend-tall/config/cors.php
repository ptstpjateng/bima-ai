<?php

return [

    /*
    |--------------------------------------------------------------------------
    | Cross-Origin Resource Sharing (CORS) Configuration
    |--------------------------------------------------------------------------
    |
    | Allows the BIMA-AI Next.js frontend to call the Laravel API.
    | The FRONTEND_URL environment variable is used to restrict allowed origins.
    |
    */

    'paths' => ['api/*', 'sanctum/csrf-cookie'],

    'allowed_methods' => ['*'],

    // Temporarily open for Vercel deployment.
    // Once the Vercel URL is known, replace '*' with the specific domain
    // and re-enable supports_credentials for Sanctum SPA cookie auth.
    'allowed_origins' => ['*'],

    'allowed_origins_patterns' => [],

    'allowed_headers' => ['*'],

    'exposed_headers' => [],

    'max_age' => 86400,

    'supports_credentials' => false,

];
