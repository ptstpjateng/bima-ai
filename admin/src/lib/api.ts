/**
 * Client-side admin-api fetcher.
 *
 * Always goes through the same-origin `/api/proxy/<path>` route handler so:
 *  - The httpOnly admin_token cookie can be attached server-side.
 *  - The browser never sees the JWT.
 *  - No CORS preflight on every request.
 *
 * Usage with TanStack Query:
 *
 *   useQuery({
 *     queryKey: ["dashboard-stats"],
 *     queryFn: () => apiFetch<DashboardStats>("/dashboard/stats"),
 *   });
 */

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
    message?: string
  ) {
    super(message ?? `API error ${status}`);
    this.name = "ApiError";
  }
}

type ApiFetchInit = Omit<RequestInit, "body"> & {
  body?: BodyInit | Record<string, unknown> | null;
  /** Optional querystring params; values are stringified and skipped if undefined/null/"". */
  params?: Record<string, string | number | boolean | undefined | null>;
};

export async function apiFetch<T = unknown>(
  path: string,
  init: ApiFetchInit = {}
): Promise<T> {
  // Normalize: ensure leading slash, then prefix with /api/proxy. The proxy
  // handler itself will strip /api/proxy and forward to admin-api.
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  let url = `/api/proxy${cleanPath}`;

  if (init.params) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(init.params)) {
      if (v === undefined || v === null || v === "") continue;
      qs.set(k, String(v));
    }
    const qsStr = qs.toString();
    if (qsStr) url += (url.includes("?") ? "&" : "?") + qsStr;
  }

  const headers = new Headers(init.headers);
  let body: BodyInit | null | undefined;
  if (init.body !== undefined && init.body !== null) {
    if (
      typeof init.body === "string" ||
      init.body instanceof FormData ||
      init.body instanceof Blob ||
      init.body instanceof ArrayBuffer ||
      init.body instanceof URLSearchParams ||
      init.body instanceof ReadableStream
    ) {
      body = init.body;
    } else {
      body = JSON.stringify(init.body);
      if (!headers.has("content-type")) {
        headers.set("content-type", "application/json");
      }
    }
  }

  const res = await fetch(url, {
    ...init,
    headers,
    body,
    // Same-origin; cookies (including admin_token) attach automatically.
    credentials: "same-origin",
  });

  const contentType = res.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");
  const payload = isJson
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);

  if (!res.ok) {
    throw new ApiError(res.status, payload);
  }
  return payload as T;
}
