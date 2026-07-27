export interface ApiResponseError extends Record<string, unknown> {
  status: number
  message: string
}

const MAX_PLAIN_TEXT_ERROR_LENGTH = 500

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isHtmlResponse(value: string, contentType: string): boolean {
  const normalizedType = contentType.toLowerCase()
  const normalizedValue = value.trimStart()
  return (
    normalizedType.includes('text/html')
    || /^<!doctype\s+html/i.test(normalizedValue)
    || /^<html(?:\s|>)/i.test(normalizedValue)
  )
}

function fallbackErrorMessage(status: number): string {
  if (status >= 500 || status === 0) {
    return 'The server is temporarily unavailable. Please try again.'
  }
  return 'The request could not be completed. Please try again.'
}

function safeMessage(
  value: unknown,
  contentType: string,
): string | null {
  if (typeof value !== 'string') return null
  const message = value.trim()
  if (
    !message
    || message.length > MAX_PLAIN_TEXT_ERROR_LENGTH
    || isHtmlResponse(message, contentType)
  ) {
    return null
  }
  return message
}

export function parseResponsePayload(raw: string): unknown {
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return raw
  }
}

export function buildApiResponseError(
  status: number,
  payload: unknown,
  contentType = '',
): ApiResponseError {
  const fallback = fallbackErrorMessage(status)
  if (!isRecord(payload)) {
    return {
      status,
      message: safeMessage(payload, contentType) ?? fallback,
    }
  }

  return {
    ...payload,
    status,
    message: safeMessage(payload.message, contentType) ?? fallback,
  }
}
