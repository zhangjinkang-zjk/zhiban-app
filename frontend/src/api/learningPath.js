import request from './request'
import { apiFetchHeaders } from './config'

export function getCurrentLearningPath() {
  return request.get('/learning_path/current')
}

export function completeLearningPathNode(nodeId, sessionId, answers = null, correctAnswers = null) {
  const data = { session_id: sessionId }
  if (answers && typeof answers === 'object' && Object.keys(answers).length > 0) {
    data.answers = answers
  }
  if (correctAnswers && typeof correctAnswers === 'object' && Object.keys(correctAnswers).length > 0) {
    data.correct_answers = correctAnswers
  }
  return request({
    url: `/learning_path/nodes/${nodeId}/complete`,
    method: 'post',
    data
  })
}

export function generateLearningPath(data) {
  return request({
    url: '/path/generate',
    method: 'post',
    data
  })
}

export async function generateLearningPathStream(data, onEvent, onError, options = {}) {
  const token = localStorage.getItem('token')
  const baseURL = request.defaults.baseURL || ''
  const url = `${baseURL}/path/generate/stream`
  const timeoutMs = Number(options.timeoutMs || 45000)
  const controller = typeof AbortController !== 'undefined' ? new AbortController() : null
  const setTimer = typeof window !== 'undefined' ? window.setTimeout.bind(window) : setTimeout
  const clearTimer = typeof window !== 'undefined' ? window.clearTimeout.bind(window) : clearTimeout
  let inactivityTimer = null

  const clearInactivityTimer = () => {
    if (!inactivityTimer) return
    clearTimer(inactivityTimer)
    inactivityTimer = null
  }

  const resetInactivityTimer = () => {
    if (!controller || !timeoutMs) return
    clearInactivityTimer()
    inactivityTimer = setTimer(() => controller.abort(), timeoutMs)
  }

  try {
    resetInactivityTimer()
    const response = await fetch(url, {
      method: 'POST',
      headers: apiFetchHeaders({
        'Content-Type': 'application/json',
        ...(token ? { token } : {})
      }),
      body: JSON.stringify(data || {}),
      ...(controller ? { signal: controller.signal } : {})
    })

    if (!response.ok) {
      const detail = await response.text().catch(() => '')
      throw new Error(detail || `HTTP ${response.status}`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      resetInactivityTimer()

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const jsonStr = line.slice(6).trim()
        if (!jsonStr || jsonStr === '[DONE]') continue
        try {
          const parsed = JSON.parse(jsonStr)
          resetInactivityTimer()
          onEvent?.(parsed)
        } catch {
          // skip unparseable event
        }
      }
    }
  } catch (err) {
    const streamError = err?.name === 'AbortError'
      ? new Error('学习路径生成响应超时，正在尝试普通生成。')
      : err
    onError?.(streamError)
    throw streamError
  } finally {
    clearInactivityTimer()
  }
}

export function generateLearningPathsFromProfile(data = {}) {
  return request({
    url: '/path/generate-from-profile',
    method: 'post',
    data
  })
}

export function getLearningPaths() {
  return request.get('/path/list')
}

export function getLearningPathDetail(pathId) {
  return request.get(`/path/${pathId}`)
}

export function getLearningPathProgress(pathId) {
  return request.get(`/path/${pathId}/progress`)
}

export function enrollLearningPath(pathId) {
  return request({
    url: '/path/enroll',
    method: 'post',
    data: {
      path_id: pathId
    }
  })
}

export function generatePathNodeResources(pathId, nodeId) {
  return request({
    url: `/path/${pathId}/node/${nodeId}/generate-resources`,
    method: 'post'
  })
}

export async function generatePathNodeResourcesStream(pathId, nodeId, onResource, onStatus, onDone, onError) {
  const token = localStorage.getItem('token')
  const baseURL = request.defaults.baseURL || ''
  const url = `${baseURL}/path/${pathId}/node/${nodeId}/generate-resources/stream`

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: apiFetchHeaders({
        'Content-Type': 'application/json',
        ...(token ? { token } : {})
      })
    })

    if (!response.ok) {
      const detail = await response.text().catch(() => '')
      onError(new Error(detail || `HTTP ${response.status}`))
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const jsonStr = line.slice(6).trim()
        if (jsonStr === '[DONE]') continue
        try {
          const data = JSON.parse(jsonStr)
          if (data.type === 'resource') {
            onResource(data)
          } else if (data.type === 'done') {
            onDone(data)
          } else if (data.type === 'status') {
            onStatus(data)
          } else if (data.type === 'error') {
            onError(new Error(data.detail || '生成失败'))
          }
        } catch {
          // skip unparseable lines
        }
      }
    }
  } catch (err) {
    onError(err)
  }
}

export function generatePathNodeQuiz(pathId, nodeId) {
  return request({
    url: `/path/${pathId}/node/${nodeId}/generate-quiz`,
    method: 'post'
  })
}

export function getPathVideo(pathId) {
  return request.get(`/path/${pathId}/video`)
}

export function generatePathVideo(pathId) {
  return request({
    url: `/path/${pathId}/video`,
    method: 'post'
  })
}
