export const isAdminRole = role => {
  const value = String(role || '').trim().toLowerCase()
  return ['admin', 'administrator', 'super_admin', 'superadmin', 'system_admin', 'manager', 'root'].includes(value)
}

const decodeBase64Url = value => {
  const normalized = String(value || '').replace(/-/g, '+').replace(/_/g, '/')
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')
  const binary = window.atob(padded)
  const bytes = Uint8Array.from(binary, char => char.charCodeAt(0))
  return new TextDecoder().decode(bytes)
}

export const parseJwtPayload = token => {
  try {
    const payload = String(token || '').split('.')[1]
    if (!payload) return null
    return JSON.parse(decodeBase64Url(payload))
  } catch {
    return null
  }
}

export const currentUserRole = () => {
  const tokenRole = parseJwtPayload(localStorage.getItem('token'))?.role || ''
  if (tokenRole) localStorage.setItem('role', tokenRole)
  if (tokenRole) return tokenRole
  return localStorage.getItem('role') || localStorage.getItem('identity') || ''
}

export const isCurrentUserAdmin = () => isAdminRole(currentUserRole())

export const resolveUserRole = (...sources) => {
  for (const source of sources) {
    const role = source?.role || source?.identity || source?.user_role || source?.userRole || source?.permission
    if (role) return role
  }
  return currentUserRole()
}

export const saveCurrentUserRole = role => {
  const value = String(role || '').trim()
  if (!value) return ''
  localStorage.setItem('role', value)
  return value
}
