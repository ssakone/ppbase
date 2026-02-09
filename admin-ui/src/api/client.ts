const TOKEN_KEY = 'ppbase_token'

class ApiClient {
  private baseUrl: string

  constructor() {
    this.baseUrl = window.location.origin
  }

  getToken(): string | null {
    return localStorage.getItem(TOKEN_KEY)
  }

  setToken(token: string): void {
    localStorage.setItem(TOKEN_KEY, token)
  }

  clearToken(): void {
    localStorage.removeItem(TOKEN_KEY)
  }

  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const token = this.getToken()
    if (token) {
      headers['Authorization'] = token
    }

    const opts: RequestInit = { method, headers }
    if (body) {
      opts.body = JSON.stringify(body)
    }

    const res = await fetch(this.baseUrl + path, opts)

    if (res.status === 204) {
      return null as T
    }

    const data = await res.json()

    if (!res.ok) {
      throw { status: res.status, ...data }
    }

    return data as T
  }
}

export const apiClient = new ApiClient()
