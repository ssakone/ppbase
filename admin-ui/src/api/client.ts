const TOKEN_KEY = 'ppbase_token'

export interface ApiRequestOptions {
  headers?: Record<string, string>
  signal?: AbortSignal
}

export interface UploadProgress {
  loaded: number
  total: number | null
  percent: number | null
}

function parseResponsePayload(raw: string): unknown {
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return { message: raw }
  }
}

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

  buildUrl(path: string): string {
    return this.baseUrl + path
  }

  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    options: ApiRequestOptions = {},
  ): Promise<T> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...options.headers,
    }
    const token = this.getToken()
    if (token) {
      headers['Authorization'] = token
    }

    const opts: RequestInit = { method, headers, signal: options.signal }
    if (body !== undefined) {
      opts.body = JSON.stringify(body)
    }

    const res = await fetch(this.buildUrl(path), opts)

    if (res.status === 204) {
      return null as T
    }

    const data = parseResponsePayload(await res.text())

    if (!res.ok) {
      throw {
        status: res.status,
        ...(data && typeof data === 'object' ? data : { message: String(data ?? '') }),
      }
    }

    return data as T
  }

  async requestFormData<T>(
    method: string,
    path: string,
    formData: FormData,
    options: ApiRequestOptions = {},
  ): Promise<T> {
    const headers: Record<string, string> = { ...options.headers }
    const token = this.getToken()
    if (token) {
      headers['Authorization'] = token
    }

    const opts: RequestInit = { method, headers, body: formData, signal: options.signal }

    const res = await fetch(this.buildUrl(path), opts)

    if (res.status === 204) {
      return null as T
    }

    const data = parseResponsePayload(await res.text())

    if (!res.ok) {
      throw {
        status: res.status,
        ...(data && typeof data === 'object' ? data : { message: String(data ?? '') }),
      }
    }

    return data as T
  }

  requestFormDataWithProgress<T>(
    method: string,
    path: string,
    formData: FormData,
    onProgress?: (progress: UploadProgress) => void,
    signal?: AbortSignal,
  ): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      let settled = false

      const finish = (callback: () => void) => {
        if (settled) return
        settled = true
        signal?.removeEventListener('abort', abort)
        callback()
      }

      const abort = () => {
        xhr.abort()
        finish(() => reject(new DOMException('The upload was cancelled.', 'AbortError')))
      }

      if (signal?.aborted) {
        abort()
        return
      }

      xhr.open(method, this.buildUrl(path))
      const token = this.getToken()
      if (token) xhr.setRequestHeader('Authorization', token)

      xhr.upload.onprogress = (event) => {
        const total = event.lengthComputable ? event.total : null
        onProgress?.({
          loaded: event.loaded,
          total,
          percent: total && total > 0 ? Math.min(100, (event.loaded / total) * 100) : null,
        })
      }

      xhr.onerror = () => {
        finish(() => reject({ status: 0, message: 'The upload connection failed.' }))
      }
      xhr.onabort = () => {
        finish(() => reject(new DOMException('The upload was cancelled.', 'AbortError')))
      }
      xhr.onload = () => {
        const data = parseResponsePayload(xhr.responseText)
        if (xhr.status >= 200 && xhr.status < 300) {
          finish(() => resolve((xhr.status === 204 ? null : data) as T))
          return
        }
        finish(() => reject({
          status: xhr.status,
          ...(data && typeof data === 'object' ? data : { message: String(data ?? '') }),
        }))
      }

      signal?.addEventListener('abort', abort, { once: true })
      xhr.send(formData)
    })
  }
}

export const apiClient = new ApiClient()
