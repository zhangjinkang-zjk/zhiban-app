import request from './request'

const API_BASE_URL = request.defaults.baseURL || '/'

// 登录：后端 Login_User 接收 username/email/password
export function login(data) {
  return request({
    url: '/user/login_user',
    method: 'post',
    data
  })
}

// 注册：后端 Create_User 接收 username/password
export function register(data) {
  return request({
    url: '/user/create_user',
    method: 'post',
    data
  })
}

// 获取用户资料：后端 Read_User 通过 token 获取 user_id
export function getUserProfile() {
  return request({
    url: '/user/read_user',
    method: 'get'
  })
}

// 更新个人信息：后端 Update_User_Information 接收 username/major/email/phonenum/profile
export function updateUserProfile(data) {
  return request({
    url: '/user/update_user/information',
    method: 'post',
    data
  })
}

// 注销账户：后端 Delete_User 接收 password，用户身份通过 token 获取
export function deleteUser(data) {
  return request({
    url: '/user/delete_user',
    method: 'delete',
    data
  })
}

// AI 聊天信息返回
export function sendChatMessage(data) {
  if (data.chat_group_id) {
    return request.post('/ai_chat/create_msg_into_history', {
      chat_group_id: data.chat_group_id,
      user_req: data.user_req
    })
  }

  return request({
    url: '/ai_chat/create_new_history',
    method: 'post',
    data: JSON.stringify(data.user_req),
    headers: {
      'Content-Type': 'application/json'
    }
  })
}

// 获取最近历史对话列表
export function getConversationList() {
  return request.get('/ai_chat/read_history_group')
}

// 获取某条对话的完整消息
export function getConversationMessages(chatGroupId) {
  return request.get('/ai_chat/read_messages_from_history', {
    params: {
      chat_group_id: chatGroupId
    }
  })
}

const parseStreamEvent = (eventText) => {
  return eventText
    .split('\n')
    .filter(line => line.startsWith('data:'))
    .map(line => line.replace(/^data:\s*/, '').trim())
    .filter(Boolean)
}

export async function streamChatMessage(data, { onChunk, onDone, onError } = {}) {
  const isExistingConversation = Boolean(data.chat_group_id)
  const url = `${API_BASE_URL}${isExistingConversation ? 'ai_chat/stream_msg_into_history' : 'ai_chat/stream_new_history'}`
  const token = localStorage.getItem('token')

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { token } : {})
    },
    body: isExistingConversation
      ? JSON.stringify({
        chat_group_id: data.chat_group_id,
        user_req: data.user_req
      })
      : JSON.stringify(data.user_req)
  })

  if (!response.ok || !response.body) {
    throw new Error(`流式请求失败：${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()

    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split(/\r?\n\r?\n/)
    buffer = events.pop() || ''

    for (const eventText of events) {
      const payloads = parseStreamEvent(eventText)

      for (const payload of payloads) {
        if (payload === '[DONE]') {
          onDone?.({})
          continue
        }

        let eventData

        try {
          eventData = JSON.parse(payload)
        } catch (error) {
          continue
        }

        if (eventData.error) {
          onError?.(eventData.error)
          throw new Error(eventData.error)
        }

        if (eventData.content) {
          onChunk?.(eventData.content)
        }

        if (eventData.done) {
          onDone?.(eventData)
        }
      }
    }
  }
}

export function getPortrait() {
  return request.get('/ai_portrait/read_portrait')
}

export function initPortrait(data) {
  return request({
    url: '/ai_portrait/init_portrait',
    method: 'post',
    data
  })
}

export function uploadStudyMaterial(data) {
  return request({
    url: '/knowledge_base/upload',
    method: 'post',
    data
  })
}
