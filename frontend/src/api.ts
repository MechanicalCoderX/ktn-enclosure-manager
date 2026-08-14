import type {
  AuditEntry,
  BaysResponse,
  Chassis,
  Enclosure,
  IdentDuration,
} from "./types";

/**
 * Every mutating call carries this header. A cross-site form post cannot set a
 * custom header, so together with the SameSite=Strict session cookie this is
 * what makes CSRF unreachable. The server rejects mutations without it.
 */
const CSRF_HEADER = { "X-KTN-Request": "1" };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(String(detail), response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const enc = encodeURIComponent;

const post = <T>(path: string, body?: unknown): Promise<T> =>
  request<T>(path, {
    method: "POST",
    headers: CSRF_HEADER,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const api = {
  authStatus: () =>
    request<{
      auth_required: boolean;
      anonymous_ident_allowed: boolean;
      needs_bootstrap: boolean;
      user: string | null;
    }>("/api/auth/status"),
  bootstrap: (username: string, password: string) =>
    post<{ ok: boolean }>("/api/auth/bootstrap", { username, password }),
  login: (username: string, password: string) =>
    post<{ ok: boolean; user: string }>("/api/auth/login", { username, password }),
  logout: () => post<{ ok: boolean }>("/api/auth/logout"),
  changePassword: (current_password: string, new_password: string) =>
    post<{ ok: boolean }>("/api/auth/password", { current_password, new_password }),

  enclosures: () => request<Enclosure[]>("/api/enclosures"),
  // Path segments are encoded rather than interpolated raw. Enclosure ids are
  // hex today, so nothing needs escaping - but the values come from a server
  // response, and a path built by concatenation is one odd id away from
  // addressing a different route than intended.
  bays: (id: string) => request<BaysResponse>(`/api/enclosures/${enc(id)}/bays`),
  chassis: (id: string) => request<Chassis>(`/api/enclosures/${enc(id)}/chassis`),
  identify: (id: string, slot: number, on: boolean, duration_seconds: IdentDuration) =>
    post<{ ok: boolean; locate: boolean; expires_at: string | null; origin: string | null }>(
      `/api/enclosures/${enc(id)}/slots/${slot}/identify`,
      { on, duration_seconds },
    ),

  diagnostics: () => request<Record<string, unknown>>("/api/diagnostics"),
  audit: (limit = 100) => request<AuditEntry[]>(`/api/audit?limit=${limit}`),
  rawPages: () => request<string[]>("/api/raw/pages"),
  rawPage: (id: string, page: string) =>
    request<{ page: string; output: string }>(`/api/raw/${enc(id)}/${enc(page)}`),
};
