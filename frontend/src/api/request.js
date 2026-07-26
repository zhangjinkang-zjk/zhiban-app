import axios from 'axios'
import { parseJwtPayload } from '../utils/auth'

export const DEFAULT_API_BASE_URL = 'http://127.0.0.1:2221'
const rawApiBaseURL = import.meta.env.VITE_API_BASE_URL?.trim() || DEFAULT_API_BASE_URL
export const apiBaseURL = rawApiBaseURL.replace(/\/+$/, '')

const AUTH_EXPIRED_MESSAGE = '\u767b\u5f55\u5df2\u8fc7\u671f\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u3002'

export const isBackendUnavailableError = error => {
  const status = Number(error?.response?.status || error?.status || 0)
  return status === 502 || status === 503 || status === 504
}

const publicUrls = ['/user/create_user', '/user/login_user', '/user/send_email_code', '/user/register_by_email', '/user/login_by_email']

const isPublicUrl = url => {
  const path = String(url || '').split('?')[0]
  return publicUrls.includes(path)
}

const request = axios.create({
  baseURL: apiBaseURL || '',
  timeout: 300000
})

let authExpiredNotified = false
let authExpiryTimer = null

const clearAuthStorage = () => {
  if (authExpiryTimer) {
    window.clearTimeout(authExpiryTimer)
    authExpiryTimer = null
  }

  localStorage.removeItem('token')
  localStorage.removeItem('user_id')
  localStorage.removeItem('username')
  localStorage.removeItem('role')
  localStorage.removeItem('identity')
  localStorage.removeItem('avatar')
  localStorage.removeItem('zhiban_generation_tasks_v2')
  localStorage.removeItem('zhiban_active_generation_task_id')
}

const notifyAuthExpired = () => {
  if (authExpiredNotified) return
  authExpiredNotified = true

  clearAuthStorage()

  window.dispatchEvent(new CustomEvent('zhiban-auth-expired', { detail: { message: AUTH_EXPIRED_MESSAGE } }))
  window.dispatchEvent(new CustomEvent('zhiban:user-logged-out', { detail: { reason: 'auth-expired' } }))

  window.setTimeout(() => {
    authExpiredNotified = false
  }, 2000)
}

const looksLikeJwt = token => {
  return typeof token === 'string' && token.split('.').length === 3
}

const scheduleAuthExpiryCheck = token => {
  if (authExpiryTimer) {
    window.clearTimeout(authExpiryTimer)
    authExpiryTimer = null
  }

  const payload = parseJwtPayload(token)
  const expiresAt = Number(payload?.exp || 0) * 1000
  if (!expiresAt) return

  const delay = expiresAt - Date.now()
  authExpiryTimer = window.setTimeout(() => {
    if (localStorage.getItem('token') === token) {
      notifyAuthExpired()
    }
  }, Math.max(0, delay))
}

const extractTokenFromResponse = data => {
  return [
    data?.token,
    data?.id,
    data?.user_id,
    data?.data?.token,
    data?.data?.id,
    data?.data?.user_id,
    data?.user?.token,
    data?.user?.id,
    data?.user?.user_id
  ].find(looksLikeJwt) || ''
}

request.interceptors.request.use(
  config => {
    config.headers = config.headers || {}
    config.headers['ngrok-skip-browser-warning'] = 'true'

    const token = localStorage.getItem('token')
    config.__hadAuthToken = Boolean(token)
    if (token) scheduleAuthExpiryCheck(token)

    if (token && !isPublicUrl(config.url)) {
      config.headers.token = token
    }

    return config
  },
  error => Promise.reject(error)
)

request.interceptors.response.use(
  response => {
    const data = response.data
    const token = extractTokenFromResponse(data)
    if (token) {
      localStorage.setItem('token', token)
      scheduleAuthExpiryCheck(token)
    }
    return data
  },
  error => {
    const status = error.response?.status
    const hadAuthToken = Boolean(error.config?.__hadAuthToken || error.config?.headers?.token || localStorage.getItem('token'))

    if (status === 401 && hadAuthToken && !isPublicUrl(error.config?.url)) {
      notifyAuthExpired()
      console.error(AUTH_EXPIRED_MESSAGE)
    }

    if (status === 422) {
      console.error('[422] request validation failed\n  URL:', error.config?.url, '\n  Method:', error.config?.method, '\n  Params:', error.config?.params, '\n  Data:', error.config?.data)
    }

    return Promise.reject(error)
  }
)

const bindAuthEventListeners = () => {
  if (window.__zhibanRequestAuthListenerCleanup) {
    window.__zhibanRequestAuthListenerCleanup()
  }

  const handleLoginSuccess = () => {
    scheduleAuthExpiryCheck(localStorage.getItem('token'))
  }

  const handleStorage = event => {
    if (event.key === 'token') scheduleAuthExpiryCheck(event.newValue)
  }

  window.addEventListener('zhiban-login-success', handleLoginSuccess)
  window.addEventListener('storage', handleStorage)
  window.__zhibanRequestAuthListenerCleanup = () => {
    window.removeEventListener('zhiban-login-success', handleLoginSuccess)
    window.removeEventListener('storage', handleStorage)
  }
}

if (typeof window !== 'undefined') {
  scheduleAuthExpiryCheck(localStorage.getItem('token'))
  bindAuthEventListeners()
}

export default request
