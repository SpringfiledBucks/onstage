export type RecordData = Record<string, any>;
declare global {
    interface Window {
        ONSTAGE_CONFIG?: {
            apiBaseUrl?: string;
            wsBaseUrl?: string;
        };
    }
}
export class ApiError extends Error {
    constructor(message: string, public status: number) { super(message); }
}
export function apiUrl(path: string) { return `${(window.ONSTAGE_CONFIG?.apiBaseUrl || '').replace(/\/$/, '')}${path}`; }
export class ApiClient {
    csrf = '';
    async request(path: string, method = 'GET', body?: unknown): Promise<RecordData> {
        if (method === 'POST' && (path === '/api/auth/register' || path === '/api/auth/login'))
            await this.request('/api/session', 'POST', {});
        const headers: Record<string, string> = { Accept: 'application/json' };
        if (body !== undefined)
            headers['Content-Type'] = 'application/json';
        if (method !== 'GET' && this.csrf)
            headers['X-CSRF-Token'] = this.csrf;
        const response = await fetch(apiUrl(path), { method, headers, credentials: 'include', cache: 'no-store', body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(20000) });
        const data = await response.json().catch(() => ({ detail: `服务器响应异常 (${response.status})` }));
        if (!response.ok)
            throw new ApiError(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`, response.status);
        if (data.csrf_token || data.csrf)
            this.csrf = data.csrf_token || data.csrf;
        return data;
    }
    async bootstrap() { try {
        return await this.request('/api/auth/me');
    }
    catch (e) {
        if (e instanceof ApiError && e.status === 401) {
            this.csrf = '';
            return { user: null };
        }
        throw e;
    } }
}
export function pageItems(data: RecordData, key: string) { return data[key] || data.items || []; }
