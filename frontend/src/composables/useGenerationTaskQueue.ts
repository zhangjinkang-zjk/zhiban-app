import { reactive } from 'vue'
import {
  createResourceGenerationTask,
  generatePresentation,
  getPresentation,
  getPresentationQuestions,
  getResourceGenerationTask,
  getResourceGenerationTasks,
  isBackendUnavailableError,
  streamPresentationProgress,
  streamResourceGenerationTask,
} from '../api/apis'
import { executeGeneration, type ResourceToolConfig } from './useResourceGeneration'

export type GenerationTaskStatus = 'running' | 'done' | 'failed'
export type AgentFlowStatus = 'pending' | 'running' | 'reviewing' | 'retrying' | 'saving' | 'done' | 'failed'

export interface AgentFlowNode {
  agent_id: string
  agent_name: string
  phase: string
  status: AgentFlowStatus
  message: string
  resource_type?: string
  current?: number
  total?: number
  elapsed_ms?: number
  updatedAt: number
}

export interface AgentFlowState {
  visible: boolean
  activeAgentId: string
  nodes: Record<string, AgentFlowNode>
  events: Array<Record<string, any>>
  updatedAt: number
}

export interface GenerationTask {
  id: string
  ownerUserId?: string
  backendTaskId: string
  text: string
  tool: ResourceToolConfig
  chatGroupId: number | string | null
  status: GenerationTaskStatus
  progress: string
  thinkingProcess: string
  error: string
  files: unknown[]
  images: unknown[]
  agentFlow?: AgentFlowState
  doneEvent: unknown
  createdAt: number
  updatedAt: number
}

const tasks = reactive<GenerationTask[]>([])
const pollingTaskIds = new Set<string>()
const streamingTaskIds = new Set<string>()
const GENERATION_TASKS_STORAGE_KEY = 'zhiban_generation_tasks_v2'
const ACTIVE_GENERATION_TASK_KEY = 'zhiban_active_generation_task_id'
const STREAM_UI_FLUSH_MS = 500
let hasHydratedTasks = false
let hydratePromise: Promise<GenerationTask[]> | null = null
let activeTaskOwnerId = ''

const currentStoredUserId = () => {
  if (typeof window === 'undefined') return ''
  return String(window.localStorage.getItem('user_id') || '').trim()
}

const taskStorageKey = () => {
  const ownerId = activeTaskOwnerId || currentStoredUserId()
  return ownerId ? `${GENERATION_TASKS_STORAGE_KEY}:${ownerId}` : GENERATION_TASKS_STORAGE_KEY
}

const clearTaskRuntimeState = () => {
  tasks.splice(0, tasks.length)
  pollingTaskIds.clear()
  streamingTaskIds.clear()
  hasHydratedTasks = false
  hydratePromise = null
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem(ACTIVE_GENERATION_TASK_KEY)
  }
}

const syncTaskOwnerContext = () => {
  const nextOwnerId = currentStoredUserId()
  if (nextOwnerId === activeTaskOwnerId) return
  clearTaskRuntimeState()
  activeTaskOwnerId = nextOwnerId
}

const currentTaskOwnerId = () => {
  syncTaskOwnerContext()
  return activeTaskOwnerId
}

const taskBelongsToCurrentUser = (task: Partial<GenerationTask> | null | undefined) => {
  const ownerId = currentTaskOwnerId()
  if (!ownerId) return false
  return String(task?.ownerUserId || '') === ownerId
}

activeTaskOwnerId = currentStoredUserId()

const isViewingGenerationPage = () => {
  if (typeof window === 'undefined' || typeof document === 'undefined') return false
  const route = window.location.hash || window.location.pathname || ''
  return !document.hidden && route.includes('/chat')
}

const dispatchNotificationUpdateIfAway = () => {
  if (!isViewingGenerationPage()) {
    window.dispatchEvent(new CustomEvent('zhiban:notification-update'))
  }
}

const RESOURCE_AGENT_LABELS: Record<string, string> = {
  ppt: 'PPT生成智能体',
  document: '文档生成智能体',
  mindmap: '思维导图智能体',
  exercise: '习题生成智能体',
  image: '图片生成智能体',
  video: '视频生成智能体',
  case: '案例资料智能体',
  reading: '阅读材料智能体',
}

const normalizeAgentStatus = (status: any): AgentFlowStatus => {
  const value = String(status || 'pending').toLowerCase()
  if (['running', 'reviewing', 'retrying', 'saving', 'done', 'failed'].includes(value)) {
    return value as AgentFlowStatus
  }
  return 'pending'
}

const FLOW_PHASES = ['leader', 'executor', 'reviewer', 'saver', 'complete']
const FLOW_PHASE_NAMES: Record<string, string> = {
  leader: 'LeaderAgent',
  executor: 'ExecutorAgent',
  reviewer: 'ReviewerAgent',
  saver: 'ResourceService',
  complete: '完成',
}
const FLOW_PHASE_DONE_MESSAGES: Record<string, string> = {
  leader: '需求规划完成',
  executor: '资源生成完成',
  reviewer: '质量审核完成',
  saver: '资源保存完成',
  complete: '生成完成',
}
const FLOW_PHASE_FAILED_MESSAGES: Record<string, string> = {
  leader: '需求规划失败',
  executor: '资源生成失败',
  reviewer: '质量审核失败',
  saver: '资源保存失败',
  complete: '生成失败',
}
const ACTIVE_AGENT_STATUSES = ['running', 'reviewing', 'retrying', 'saving']

const makeAgentNode = (
  agentId: string,
  agentName: string,
  phase: string,
  status: AgentFlowStatus = 'pending',
  message = '等待中',
): AgentFlowNode => ({
  agent_id: agentId,
  agent_name: agentName,
  phase,
  status,
  message,
  updatedAt: Date.now(),
})

const isActiveAgentStatus = (status: AgentFlowStatus | string) => {
  return ACTIVE_AGENT_STATUSES.includes(String(status))
}

const phaseRank = (phase: string) => FLOW_PHASES.indexOf(phase)

const ensurePhaseNode = (flow: AgentFlowState, phase: string, now = Date.now()) => {
  const existing = flow.nodes[phase]
  if (existing) return existing
  const node = makeAgentNode(
    phase,
    FLOW_PHASE_NAMES[phase] || phase,
    phase,
    'pending',
    '等待中',
  )
  node.updatedAt = now
  flow.nodes[phase] = node
  return node
}

const markPreviousPhasesDone = (flow: AgentFlowState, phase: string, now = Date.now()) => {
  const rank = phaseRank(phase)
  if (rank <= 0) return
  for (const previousPhase of FLOW_PHASES.slice(0, rank)) {
    const node = ensurePhaseNode(flow, previousPhase, now)
    if (node.status === 'failed') continue
    node.status = 'done'
    node.message = FLOW_PHASE_DONE_MESSAGES[previousPhase] || node.message || '已完成'
    node.updatedAt = now
  }
}

const setPhaseStatus = (
  flow: AgentFlowState,
  phase: string,
  status: AgentFlowStatus,
  message = '',
  now = Date.now(),
) => {
  if (phaseRank(phase) === -1) return
  const node = ensurePhaseNode(flow, phase, now)
  node.status = status
  node.message = message || (status === 'done'
    ? (FLOW_PHASE_DONE_MESSAGES[phase] || '已完成')
    : node.message)
  node.updatedAt = now
  if (isActiveAgentStatus(status) || phase === 'complete') {
    flow.activeAgentId = phase
  }
}

const getFlowResourceTypes = (flow: AgentFlowState, tool?: ResourceToolConfig) => {
  const fromTool = tool?.resourceTypes || []
  const fromNodes = Object.values(flow.nodes)
    .map(node => node.resource_type || (node as any).resourceType)
    .filter(Boolean)
  return [...new Set([...fromTool, ...fromNodes].map(item => String(item || '').toLowerCase()))]
    .filter(item => item && item !== 'external_video')
}

const syncResourceBranchFromChildren = (
  flow: AgentFlowState,
  resourceType: string,
  now = Date.now(),
) => {
  if (!resourceType) return
  const branchId = `executor:${resourceType}`
  const branch = flow.nodes[branchId]
  if (!branch) return
  const children = Object.values(flow.nodes).filter(node => (
    node.phase === 'executor' &&
    String(node.resource_type || (node as any).resourceType || '').toLowerCase() === resourceType &&
    String(node.agent_id || (node as any).agentId || '').includes(':section-')
  ))
  if (!children.length) return

  const failed = children.find(node => normalizeAgentStatus(node.status) === 'failed')
  const active = children.find(node => isActiveAgentStatus(normalizeAgentStatus(node.status)))
  const doneCount = children.filter(node => normalizeAgentStatus(node.status) === 'done').length
  const total = Math.max(Number(branch.total || 0), children.length)

  const previousStatus = normalizeAgentStatus(branch.status)
  if (previousStatus === 'done' && !failed) {
    branch.current = Math.max(Number(branch.current || 0), Number(branch.total || 0), doneCount)
    branch.total = Math.max(Number(branch.total || 0), total || 0) || branch.total
    branch.updatedAt = now
    return
  }

  if (failed) {
    branch.status = 'failed'
    branch.message = failed.message || '章节生成失败'
  } else if (active) {
    branch.status = normalizeAgentStatus(active.status)
    branch.message = active.message || '正在并行生成章节'
  } else if (doneCount >= total && total > 0) {
    branch.status = 'done'
    branch.message = '章节生成完成'
  } else if (branch.status === 'pending') {
    branch.status = 'running'
    branch.message = `已完成 ${doneCount}/${total || children.length} 章节`
  }

  branch.current = Math.max(Number(branch.current || 0), doneCount)
  branch.total = total || branch.total
  branch.updatedAt = now
}

const syncExecutorPhaseFromBranches = (
  flow: AgentFlowState,
  tool?: ResourceToolConfig,
  now = Date.now(),
) => {
  const resourceTypes = getFlowResourceTypes(flow, tool)
  const branches = resourceTypes
    .map(type => flow.nodes[`executor:${type}`])
    .filter(Boolean)
  if (!branches.length) return

  const failed = branches.find(node => normalizeAgentStatus(node.status) === 'failed')
  const active = branches.find(node => isActiveAgentStatus(normalizeAgentStatus(node.status)))
  if (failed) {
    setPhaseStatus(flow, 'executor', 'failed', failed.message || '资源生成失败', now)
  } else if (active) {
    setPhaseStatus(flow, 'executor', normalizeAgentStatus(active.status), active.message || '正在并行生成资源', now)
  } else if (branches.every(node => normalizeAgentStatus(node.status) === 'done')) {
    setPhaseStatus(flow, 'executor', 'done', '资源生成完成', now)
  }
}

const createInitialAgentFlow = (tool?: ResourceToolConfig): AgentFlowState => {
  const nodes: Record<string, AgentFlowNode> = {
    leader: makeAgentNode('leader', 'LeaderAgent', 'leader', 'running', '正在分析需求'),
    executor: makeAgentNode('executor', 'ExecutorAgent', 'executor', 'pending', '等待生成任务'),
    reviewer: makeAgentNode('reviewer', 'ReviewerAgent', 'reviewer', 'pending', '等待审核'),
    saver: makeAgentNode('saver', 'ResourceService', 'saver', 'pending', '等待保存'),
    complete: makeAgentNode('complete', '完成', 'complete', 'pending', '等待结果'),
  }

  for (const type of tool?.resourceTypes || []) {
    const key = String(type || '').toLowerCase()
    if (!key) continue
    nodes[`executor:${key}`] = makeAgentNode(
      `executor:${key}`,
      RESOURCE_AGENT_LABELS[key] || `${key} 智能体`,
      'executor',
      'pending',
      '等待调度',
    )
    nodes[`executor:${key}`].resource_type = key
  }

  return {
    visible: true,
    activeAgentId: 'leader',
    nodes,
    events: [],
    updatedAt: Date.now(),
  }
}

const normalizeAgentFlow = (flow: any, tool?: ResourceToolConfig): AgentFlowState => {
  const base = createInitialAgentFlow(tool)
  if (!flow || typeof flow !== 'object') return base
  return {
    visible: flow.visible !== false,
    activeAgentId: flow.activeAgentId || base.activeAgentId,
    nodes: { ...base.nodes, ...(flow.nodes || {}) },
    events: Array.isArray(flow.events) ? flow.events.slice(-80) : [],
    updatedAt: Number(flow.updatedAt || Date.now()),
  }
}

const finishedNodeMessage = (node: AgentFlowNode, failed: boolean) => {
  if (failed) {
    return FLOW_PHASE_FAILED_MESSAGES[node.phase] || '生成失败'
  }
  if (FLOW_PHASE_DONE_MESSAGES[node.phase]) {
    return FLOW_PHASE_DONE_MESSAGES[node.phase]
  }
  const resourceType = String(node.resource_type || (node as any).resourceType || '').toLowerCase()
  if (node.phase === 'executor' && resourceType) {
    const label = RESOURCE_AGENT_LABELS[resourceType] || `${resourceType}智能体`
    return `${label.replace(/智能体$/u, '')}完成`
  }
  return '已完成'
}

const normalizeRestoredAgentFlow = (
  flow: any,
  tool: ResourceToolConfig | undefined,
  taskStatus: GenerationTaskStatus,
): AgentFlowState => {
  const restored = normalizeAgentFlow(flow, tool)
  if (taskStatus === 'running') return restored

  const now = Date.now()
  const finalStatus: AgentFlowStatus = taskStatus === 'failed' ? 'failed' : 'done'
  for (const node of Object.values(restored.nodes)) {
    if (node.status !== 'failed') {
      node.status = finalStatus
      node.message = finishedNodeMessage(node, taskStatus === 'failed')
      node.updatedAt = now
    }
  }
  restored.nodes.complete = {
    ...(restored.nodes.complete || makeAgentNode('complete', '完成', 'complete')),
    status: finalStatus,
    message: taskStatus === 'failed' ? '生成失败' : '生成完成',
    updatedAt: now,
  }
  restored.events = Array.isArray(restored.events) && restored.events.length
    ? restored.events
    : [{
        type: 'agent_event',
        agent_id: 'complete',
        agent_name: '完成',
        phase: 'complete',
        status: finalStatus,
        message: taskStatus === 'failed' ? '生成失败' : '生成完成',
        timestamp: now,
      }]
  restored.activeAgentId = 'complete'
  restored.visible = true
  restored.updatedAt = now
  return restored
}

const isCompletedPresentationPayload = (value: any) => {
  if (!value || typeof value !== 'object') return false
  const fileUrl = value.file_url || value.fileUrl || value.preview_url || value.previewUrl || value.presentation_url || value.presentationUrl
  return Boolean(fileUrl)
}

const hasCompletedPresentation = (value: any) => {
  const files = Array.isArray(value?.files) ? value.files : []
  const fromFiles = files.some((file: any) => {
    const resourceKind = String(file?.resourceKind || file?.kind || '').toLowerCase()
    const fileType = String(file?.file_type || file?.fileType || file?.resource_type || file?.resourceType || '').toLowerCase()
    return (resourceKind === 'presentation' || fileType === 'video') && isCompletedPresentationPayload(file)
  })
  return fromFiles || isCompletedPresentationPayload(value?.doneEvent?.presentation)
}

const persistTasks = () => {
  try {
    const ownerId = currentTaskOwnerId()
    if (!ownerId) return
    const ownedTasks = tasks.filter(task => taskBelongsToCurrentUser(task))
    window.localStorage.setItem(
      taskStorageKey(),
      JSON.stringify(ownedTasks.slice(0, 30).map(task => ({
        id: task.id,
        ownerUserId: task.ownerUserId || ownerId,
        backendTaskId: task.backendTaskId,
        text: task.text,
        tool: task.tool,
        chatGroupId: task.chatGroupId,
        status: task.status,
        progress: task.progress,
        thinkingProcess: task.thinkingProcess,
        error: task.error,
        files: task.files,
        images: task.images,
        agentFlow: task.agentFlow,
        doneEvent: task.doneEvent,
        createdAt: task.createdAt,
        updatedAt: task.updatedAt,
      }))),
    )
    window.localStorage.removeItem(GENERATION_TASKS_STORAGE_KEY)
  } catch {
    // localStorage may be unavailable in private browsing or during SSR-like tests.
  }
}

const restorePersistedTasks = () => {
  try {
    const ownerId = currentTaskOwnerId()
    if (!ownerId) return
    const raw = window.localStorage.getItem(taskStorageKey())
    const list = raw ? JSON.parse(raw) : []
    if (!Array.isArray(list)) return

    list.forEach(item => {
      if (String(item?.ownerUserId || ownerId) !== ownerId) return
      if (!item?.id || tasks.some(task => task.id === item.id)) return
      const isRecent = Date.now() - Number(item.updatedAt || 0) < 60 * 60 * 1000
      if (item.status !== 'running' && !isRecent) return
      // 超过 30 分钟的 running 任务视为已失效，不再恢复轮询
      const stale = Date.now() - Number(item.createdAt || 0) > 30 * 60 * 1000
      if (item.status === 'running' && stale) return
      const status = item.tool?.generateMode === 'video' && item.status !== 'failed' && hasCompletedPresentation(item)
        ? 'done'
        : (item.status || 'running')
      tasks.push(reactive({
        id: item.id,
        ownerUserId: ownerId,
        backendTaskId: item.backendTaskId || '',
        text: item.text || '',
        tool: item.tool || { label: 'resource', generateMode: 'resource', resourceTypes: ['document'] },
        chatGroupId: item.chatGroupId ?? null,
        status,
        progress: item.progress || '正在生成资源...',
        thinkingProcess: item.thinkingProcess || '',
        error: item.error || '',
        files: Array.isArray(item.files) ? item.files : [],
        images: Array.isArray(item.images) ? item.images : [],
        agentFlow: normalizeRestoredAgentFlow(item.agentFlow, item.tool, status),
        doneEvent: item.doneEvent || null,
        createdAt: Number(item.createdAt || Date.now()),
        updatedAt: Number(item.updatedAt || Date.now()),
      }) as GenerationTask)
    })
  } catch {
    // Ignore malformed cache.
  }
}

const resetTaskQueueForCurrentUser = () => {
  clearTaskRuntimeState()
  activeTaskOwnerId = currentStoredUserId()
  try {
    window.localStorage.removeItem(GENERATION_TASKS_STORAGE_KEY)
  } catch {
    // Ignore storage cleanup failures.
  }
}

type GenerationTaskQueueWindow = Window & {
  __zhibanGenerationTaskQueueCleanup?: () => void
}

const bindTaskQueueEventListeners = () => {
  const browserWindow = window as GenerationTaskQueueWindow
  browserWindow.__zhibanGenerationTaskQueueCleanup?.()

  const handleStorage = (event: StorageEvent) => {
    if (event.key === 'user_id' || event.key === 'token') {
      syncTaskOwnerContext()
    }
  }

  window.addEventListener('zhiban-login-success', resetTaskQueueForCurrentUser)
  window.addEventListener('zhiban:user-logged-out', resetTaskQueueForCurrentUser)
  window.addEventListener('zhiban-auth-expired', resetTaskQueueForCurrentUser)
  window.addEventListener('storage', handleStorage)

  browserWindow.__zhibanGenerationTaskQueueCleanup = () => {
    window.removeEventListener('zhiban-login-success', resetTaskQueueForCurrentUser)
    window.removeEventListener('zhiban:user-logged-out', resetTaskQueueForCurrentUser)
    window.removeEventListener('zhiban-auth-expired', resetTaskQueueForCurrentUser)
    window.removeEventListener('storage', handleStorage)
  }
}

if (typeof window !== 'undefined') {
  bindTaskQueueEventListeners()
}

restorePersistedTasks()

const unwrapResponseData = (result: any) => result?.data?.data ?? result?.data ?? result
const wait = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))

const waitForPresentationFile = async (
  initial: any,
  onProgress?: (message: string) => void,
  maxAttempts = 90,
) => {
  let presentation = initial || {}
  const id = presentation?.id || presentation?.presentation_id || presentation?.presentationId
  if (!id) return presentation
  if (presentation?.file_url || presentation?.fileUrl) return presentation

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    await wait(attempt <= 3 ? 1000 : 2000)
    const detail = unwrapResponseData(await getPresentation(id))
    presentation = { ...presentation, ...detail }
    const fileUrl = presentation?.file_url || presentation?.fileUrl
    if (fileUrl) return presentation
    if (presentation?.status === 'failed') {
      throw new Error(presentation?.error_message || presentation?.errorMessage || '学习视频生成失败')
    }
    if (attempt === 1 || attempt % 5 === 0) {
      onProgress?.('学习视频骨架生成中，请稍等...')
    }
  }

  throw new Error('学习视频仍在生成中，暂时没有拿到可预览文件')
}

const makeLocalTaskId = (taskId = '') => taskId ? `generation-${taskId}` : `generation-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

const toFrontendStatus = (status: string): GenerationTaskStatus => {
  if (status === 'success' || status === 'done') return 'done'
  if (status === 'failed') return 'failed'
  return 'running'
}

const formatTaskProgress = (taskData: any) => {
  const message = taskData?.progress_msg || taskData?.progressMsg || ''
  const progress = Number(taskData?.progress || 0)
  if (message && progress) return `${message} ${progress}%`
  if (message) return message
  if (progress) return `正在生成资源 ${progress}%...`
  return '正在生成资源...'
}

const formatTaskThinking = (value: any) => {
  const raw = Array.isArray(value)
    ? value.join('\n')
    : String(value || '')
  return raw
    .replace(/!\[[^\]]*\]\([^)]+\)/g, '')
    .split(/\r?\n/)
    .map(line => line.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join('\n')
}

const getBackendThinking = (taskData: any) => formatTaskThinking(
  taskData?.progress_msg ||
  taskData?.progressMsg ||
  taskData?.message ||
  taskData?.msg
)

const appendTaskThinking = (task: GenerationTask, value: any) => {
  const next = formatTaskThinking(value)
  if (!next) return

  const lines = formatTaskThinking(task.thinkingProcess)
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)

  for (const line of next.split(/\r?\n/).map(item => item.trim()).filter(Boolean)) {
    if (lines[lines.length - 1] !== line) {
      lines.push(line)
    }
  }

  task.thinkingProcess = lines.slice(-80).join('\n')
}

const shouldFlushStreamUi = (task: GenerationTask, key: string, immediate = false) => {
  const now = Date.now()
  const state = ((task as any)._streamUiFlush ||= {})
  if (immediate || now - Number(state[key] || 0) >= STREAM_UI_FLUSH_MS) {
    state[key] = now
    return true
  }
  return false
}

const touchTask = (task: GenerationTask) => {
  task.updatedAt = Date.now()
  persistTasks()
}

const applyStreamProgressMessage = (task: GenerationTask, eventData: any) => {
  const message = eventData?.progress_msg || eventData?.progressMsg || eventData?.message
  if (message) task.progress = message
}

const upsertAgentFlowNode = (task: GenerationTask, eventData: any) => {
  const flow = normalizeAgentFlow((task as any).agentFlow, task.tool)
  const now = Date.now()
  const agentId = String(eventData.agent_id || eventData.agentId || eventData.phase || `agent-${now}`)
  const phase = String(eventData.phase || (agentId.split(':')[0] || 'executor'))
  const resourceType = String(eventData.resource_type || eventData.resourceType || '').toLowerCase()
  const status = normalizeAgentStatus(eventData.status)
  const completeStatus = normalizeAgentStatus(flow.nodes.complete?.status)
  if (
    ['done', 'failed'].includes(completeStatus) &&
    phase !== 'complete' &&
    status !== 'failed'
  ) {
    return
  }
  const previous = flow.nodes[agentId] || makeAgentNode(agentId, eventData.agent_name || eventData.agentName || agentId, phase)
  const isSectionAgent = agentId.includes(':section-')
  const previousStatus = normalizeAgentStatus(previous.status)
  const incomingCurrent = eventData.current != null ? Number(eventData.current) : undefined
  const incomingTotal = eventData.total != null ? Number(eventData.total) : undefined
  const previousCurrent = previous.current != null ? Number(previous.current) : undefined
  const previousTotal = previous.total != null ? Number(previous.total) : undefined
  const nextCurrent = Math.max(
    Number.isFinite(previousCurrent) ? previousCurrent as number : 0,
    Number.isFinite(incomingCurrent) ? incomingCurrent as number : 0,
  ) || undefined
  const nextTotal = Math.max(
    Number.isFinite(previousTotal) ? previousTotal as number : 0,
    Number.isFinite(incomingTotal) ? incomingTotal as number : 0,
  ) || undefined
  const nextStatus = previousStatus === 'done' && status !== 'failed' ? 'done' : status

  markPreviousPhasesDone(flow, phase, now)
  if (phaseRank(phase) !== -1) {
    if (isActiveAgentStatus(nextStatus) || agentId === phase || phase === 'saver' || phase === 'complete') {
      setPhaseStatus(flow, phase, nextStatus, eventData.message || previous.message || '', now)
    }
  }

  const nextNode: AgentFlowNode = {
    ...previous,
    ...eventData,
    agent_id: agentId,
    agent_name: eventData.agent_name || eventData.agentName || previous.agent_name || agentId,
    phase,
    status: nextStatus,
    message: eventData.message || previous.message || '',
    resource_type: resourceType || previous.resource_type,
    current: nextCurrent ?? previous.current,
    total: nextTotal ?? previous.total,
    updatedAt: now,
  }

  flow.nodes[agentId] = nextNode

  if (phase === 'executor' && resourceType) {
    const branchId = `executor:${resourceType}`
    const branchPrevious = flow.nodes[branchId] || makeAgentNode(
      branchId,
      RESOURCE_AGENT_LABELS[resourceType] || `${resourceType} 智能体`,
      'executor',
    )
    const branchStatus = normalizeAgentStatus(branchPrevious.status)
    const shouldWriteBranchStatus = branchStatus !== 'done' && (!isSectionAgent || isActiveAgentStatus(nextStatus) || nextStatus === 'failed')
    const branchCurrent = Math.max(
      Number(branchPrevious.current || 0),
      Number(eventData.current || 0),
    ) || branchPrevious.current
    const branchTotal = Math.max(
      Number(branchPrevious.total || 0),
      Number(eventData.total || 0),
    ) || branchPrevious.total
    flow.nodes[branchId] = {
      ...branchPrevious,
      status: shouldWriteBranchStatus ? nextStatus : branchPrevious.status,
      message: shouldWriteBranchStatus ? nextNode.message : branchPrevious.message,
      resource_type: resourceType,
      current: branchCurrent,
      total: branchTotal,
      elapsed_ms: eventData.elapsed_ms ?? branchPrevious.elapsed_ms,
      updatedAt: now,
    }
    syncResourceBranchFromChildren(flow, resourceType, now)
    syncExecutorPhaseFromBranches(flow, task.tool, now)
  }

  if (['running', 'reviewing', 'retrying', 'saving'].includes(nextStatus)) {
    flow.activeAgentId = agentId
  }
  if (phaseRank(phase) !== -1 && (isActiveAgentStatus(nextStatus) || phase === 'complete')) {
    flow.activeAgentId = phase
  }

  flow.events = [
    ...(flow.events || []),
    {
      ...eventData,
      agent_id: agentId,
      agent_name: nextNode.agent_name,
      phase,
      status: nextStatus,
      resource_type: resourceType || nextNode.resource_type,
      updatedAt: now,
    }
  ].slice(-80)
  flow.visible = true
  flow.updatedAt = now
  ;(task as any).agentFlow = flow
}

const applyAgentFlowEvent = (task: GenerationTask, eventData: any) => {
  if (eventData?.type !== 'agent_event') return false
  upsertAgentFlowNode(task, eventData)
  return true
}

const applyBackendProgressToAgentFlow = (task: GenerationTask, eventData: any) => {
  if (!eventData || eventData.type === 'agent_event' || eventData.type === 'external_videos') return false

  const type = String(eventData.type || '').toLowerCase()
  const message = String(eventData.progress_msg || eventData.progressMsg || eventData.message || '')
  const progress = Number(eventData.progress || 0)
  const resourceType = String(eventData.resource_type || eventData.resourceType || eventData.file_type || eventData.fileType || '').toLowerCase()
  let phase = ''
  let status: AgentFlowStatus = 'running'
  let agentId = ''
  let agentName = ''
  let flowMessage = message

  if (eventData.done || type === 'done') {
    phase = 'complete'
    status = String(eventData.status || '').toLowerCase() === 'failed' ? 'failed' : 'done'
    agentId = 'complete'
    agentName = status === 'failed' ? '生成失败' : '完成'
    flowMessage = message || (status === 'failed' ? '生成失败' : '生成完成')
  } else if (eventData.error) {
    phase = 'complete'
    status = 'failed'
    agentId = 'complete'
    agentName = '生成失败'
    flowMessage = String(eventData.error || '生成失败')
  } else if (progress >= 85 || /保存/.test(message)) {
    phase = 'saver'
    status = 'saving'
    agentId = 'saver'
    agentName = 'ResourceService'
    flowMessage = message || '正在保存生成资源'
  } else if (progress >= 70 || /审核/.test(message)) {
    phase = 'reviewer'
    status = 'reviewing'
    agentId = 'reviewer'
    agentName = 'ReviewerAgent'
    flowMessage = message || '正在进行质量审核'
  } else if (
    type === 'progress' ||
    type === 'stream_progress' ||
    type === 'stream_start' ||
    type === 'stream_slide_start' ||
    type === 'stream_slide' ||
    type === 'stream_slide_done' ||
    type === 'stream_text_start' ||
    type === 'stream_text_done'
  ) {
    phase = 'executor'
    status = type === 'progress' && /生成完毕|生成完成|已生成/.test(message) ? 'done' : 'running'
    agentId = resourceType ? `executor:${resourceType}` : 'executor'
    agentName = resourceType ? (RESOURCE_AGENT_LABELS[resourceType] || `${resourceType}智能体`) : 'ExecutorAgent'
    flowMessage = message || '正在生成资源'
  } else if (progress > 0 || /初始化|规划|分析/.test(message)) {
    phase = 'leader'
    status = 'running'
    agentId = 'leader'
    agentName = 'LeaderAgent'
    flowMessage = message || '正在分析学习需求'
  }

  if (!phase) return false

  upsertAgentFlowNode(task, {
    type: 'agent_event',
    agent_id: agentId || phase,
    agent_name: agentName || FLOW_PHASE_NAMES[phase] || phase,
    phase,
    status,
    message: flowMessage,
    resource_type: resourceType || undefined,
    current: eventData.current,
    total: eventData.total,
    elapsed_ms: eventData.elapsed_ms,
    source_event_type: eventData.type,
  })
  return true
}

const finishAgentFlow = (task: GenerationTask, failed = false) => {
  const flow = normalizeAgentFlow((task as any).agentFlow, task.tool)
  const now = Date.now()
  const finalStatus: AgentFlowStatus = failed ? 'failed' : 'done'

  for (const node of Object.values(flow.nodes)) {
    if (node.status !== 'failed') {
      node.status = finalStatus
      node.message = finishedNodeMessage(node, failed)
      if (!failed && node.total != null) {
        node.current = node.total
      }
      node.updatedAt = now
    }
  }

  flow.nodes.complete = {
    ...(flow.nodes.complete || makeAgentNode('complete', '完成', 'complete')),
    status: finalStatus,
    message: failed ? '生成失败' : '生成完成',
    updatedAt: now,
  }
  flow.events = [
    ...(Array.isArray(flow.events) ? flow.events : []),
    {
      type: 'agent_event',
      agent_id: 'complete',
      agent_name: '完成',
      phase: 'complete',
      status: finalStatus,
      message: failed ? '生成失败' : '生成完成',
      timestamp: now,
    },
  ].slice(-80)
  flow.activeAgentId = 'complete'
  flow.visible = true
  flow.updatedAt = now
  ;(task as any).agentFlow = flow
}

const setAgentFlowVisibility = (task: GenerationTask, visible: boolean) => {
  const flow = normalizeAgentFlow((task as any).agentFlow, task.tool)
  flow.visible = visible
  flow.updatedAt = Date.now()
  ;(task as any).agentFlow = flow
}

const normalizeTaskFiles = (taskData: any) => {
  let result = taskData?.result ?? taskData?.resources ?? []
  if (typeof result === 'string') {
    try {
      result = JSON.parse(result)
    } catch {
      result = []
    }
  }
  if (result && typeof result === 'object' && !Array.isArray(result)) {
    result = result.resources || result.result || result.data || []
  }

  return (Array.isArray(result) ? result : []).map((item: any) => {
    const resourceId = item.file_id || item.fileId || item.resource_id || item.resourceId
    const resourceType = item.resource_type || item.resourceType || item.file_type || item.fileType
    const fileType = item.file_type || item.fileType || item.resource_type || item.resourceType
    return {
      ...item,
      file_id: resourceId,
      resource_id: item.resource_id || item.resourceId || resourceId,
      file_type: fileType,
      resource_type: resourceType,
      filename: item.filename || `${item.topic || '生成资源'}_${fileType || resourceType || 'resource'}`,
      download_url: item.download_url || item.downloadUrl || (resourceId ? `/resource/${resourceId}/download` : ''),
    }
  })
}

const applyBackendTaskData = (task: GenerationTask, taskData: any) => {
  if (!taskBelongsToCurrentUser(task)) return
  const prevStatus = task.status
  task.backendTaskId = taskData?.task_id || taskData?.taskId || task.backendTaskId
  task.chatGroupId = taskData?.chat_group_id || taskData?.chatGroupId || task.chatGroupId
  task.status = toFrontendStatus(String(taskData?.status || 'running'))
  task.progress = task.status === 'failed'
    ? (taskData?.error || '资源生成失败，请稍后再试。')
    : formatTaskProgress(taskData)
  const thinking = getBackendThinking(taskData)
  if (!(task as any)._textStream) appendTaskThinking(task, thinking)
  task.error = taskData?.error || ''
  // 保留已生成的视频文件和 doneEvent.presentation，
  // 避免 hydrate 时后端数据覆盖前端已完成的视频状态导致重复生成
  const existingPresentation = (task.doneEvent as any)?.presentation
  const existingVideoFiles = task.files.filter(
    f => (f as any).file_type === 'video' || (f as any).resource_type === 'video' || (f as any).resourceKind === 'presentation'
  )
  const backendFiles = normalizeTaskFiles(taskData)
  if (backendFiles.length || task.status === 'done' || task.status === 'failed') {
    task.files.splice(0, task.files.length, ...backendFiles)
  }
  // 把视频文件追加回去（放在资源文件后面）
  if (existingVideoFiles.length) {
    task.files.push(...existingVideoFiles)
  }
  task.doneEvent = {
    chat_group_id: task.chatGroupId,
    resources: task.files,
  }
  if (existingPresentation) {
    (task.doneEvent as any).presentation = existingPresentation
  }
  if (task.status === 'done') {
    finishAgentFlow(task, false)
  } else if (task.status === 'failed') {
    finishAgentFlow(task, true)
  }
  task.updatedAt = Date.now()
  persistTasks()

  // 任务完成/失败时通知 TopNav 刷新铃铛（模块级，不受组件卸载影响）
  if (prevStatus === 'running' && task.status !== 'running') {
    console.log('[GenerationTask] 任务状态变更，发送通知刷新事件', { taskId: task.backendTaskId, prevStatus, newStatus: task.status })
    dispatchNotificationUpdateIfAway()
  }
}

const upsertBackendTask = (taskData: any, tool?: ResourceToolConfig, ownerSnapshot = currentTaskOwnerId()) => {
  const ownerId = currentTaskOwnerId()
  if (!ownerId || ownerSnapshot !== ownerId) return null
  const backendTaskId = taskData?.task_id || taskData?.taskId || ''
  if (!backendTaskId) return null

  let task = tasks.find(item => item.backendTaskId === backendTaskId && item.ownerUserId === ownerId)
  if (!task) {
    task = reactive({
      id: makeLocalTaskId(backendTaskId),
      ownerUserId: ownerId,
      backendTaskId,
      text: taskData?.topic || '',
      tool: tool || {
        label: 'resource',
        generateMode: 'resource',
        resourceTypes: taskData?.resource_types || taskData?.resourceTypes || ['document'],
      },
      chatGroupId: taskData?.chat_group_id || taskData?.chatGroupId || null,
      status: 'running',
      progress: '正在生成资源...',
      thinkingProcess: '',
      error: '',
      files: [],
      images: [],
      doneEvent: null,
      createdAt: taskData?.created_at ? new Date(taskData.created_at).getTime() || Date.now() : Date.now(),
      updatedAt: Date.now(),
    }) as GenerationTask
    tasks.unshift(task)
  }

  applyBackendTaskData(task, taskData)
  return task
}

const pollBackendTask = (task: GenerationTask) => {
  if (!taskBelongsToCurrentUser(task)) return
  if (!task.backendTaskId || pollingTaskIds.has(task.backendTaskId)) return
  pollingTaskIds.add(task.backendTaskId)

  const tick = async () => {
    if (!taskBelongsToCurrentUser(task)) {
      pollingTaskIds.delete(task.backendTaskId)
      return
    }
    try {
      const result = await getResourceGenerationTask(task.backendTaskId)
      const data = unwrapResponseData(result)
      applyBackendTaskData(task, data)
      if (task.status === 'running') {
        window.setTimeout(tick, 1800)
      } else {
        pollingTaskIds.delete(task.backendTaskId)
      }
    } catch (error: any) {
      if (isBackendUnavailableError(error)) {
        task.progress = '后端暂时不可用，正在等待重连...'
        task.updatedAt = Date.now()
        persistTasks()
        window.setTimeout(tick, 5000)
        return
      }
      task.status = 'failed'
      task.error = error?.response?.data?.detail || error?.message || '任务状态同步失败'
      task.progress = task.error
      task.updatedAt = Date.now()
      pollingTaskIds.delete(task.backendTaskId)
    }
  }

  window.setTimeout(tick, 800)
}

const streamOrder = (value: any, fallback = Number.MAX_SAFE_INTEGER) => {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

const sortPptStreamSlides = (slides: any[]) => {
  return [...slides].sort((a, b) => {
    const sectionDiff = streamOrder(a?.section_idx) - streamOrder(b?.section_idx)
    if (sectionDiff !== 0) return sectionDiff
    return streamOrder(a?.slide_idx) - streamOrder(b?.slide_idx)
  })
}

const findPptStreamSlideIndex = (slides: any[], eventData: any) => {
  return slides.findIndex((s: any) =>
    streamOrder(s?.section_idx) === streamOrder(eventData?.section_idx) &&
    streamOrder(s?.slide_idx) === streamOrder(eventData?.slide_idx)
  )
}

const bumpPptStreamVersion = (stream: any) => {
  stream._version = Number(stream._version || 0) + 1
  return stream
}

const findTextStreamPartIndex = (parts: any[], eventData: any) => {
  const sectionIdx = streamOrder(eventData?.section_idx, 0)
  return parts.findIndex((part: any) => streamOrder(part?.section_idx, 0) === sectionIdx)
}

const sortTextStreamParts = (parts: any[]) => {
  return [...parts].sort((a, b) => streamOrder(a?.section_idx, 0) - streamOrder(b?.section_idx, 0))
}

const formatTextStreamThinking = (stream: any, task: GenerationTask) => {
  const parts = sortTextStreamParts(stream?.parts || [])
    .map((part: any) => String(part?.content || '').trim())
    .filter(Boolean)
  const types = task.tool?.resourceTypes || []
  const isMindmap = stream?.file_type === 'mindmap' || types.includes('mindmap')
  const title = task.status === 'done'
    ? (isMindmap ? '思维导图内容已生成。' : '文档内容已生成。')
    : (isMindmap ? '正在生成思维导图内容...' : '正在生成文档内容...')
  if (!parts.length) return title
  return `${title}\n\n${parts.join('\n\n')}`
}

const applyTaskStreamEvent = (task: GenerationTask, eventData: any) => {
  if (!eventData || eventData.type === '__close__') return

  if (applyAgentFlowEvent(task, eventData)) {
    task.updatedAt = Date.now()
    persistTasks()
    return
  }

  // 外部视频搜索结果（在 graph 完成前提前推送）
  if (eventData.type === 'external_videos' && Array.isArray(eventData.external_videos)) {
    if (!(task as any).externalVideos) {
      ;(task as any).externalVideos = []
    }
    ;(task as any).externalVideos.push(...eventData.external_videos)
    if (eventData.progress_msg) {
      task.progress = eventData.progress_msg
    }
    task.updatedAt = Date.now()
    persistTasks()
    return
  }

  const internalVideoStreamTypes = new Set([
    'stream_start',
    'stream_section_replace',
    'stream_slide_start',
    'stream_slide_delta',
    'stream_slide',
    'stream_slide_done',
    'stream_text_start',
    'stream_text_delta',
    'stream_text_done',
  ])
  const isVideoInternalResourceStream = task.tool?.generateMode === 'video' && internalVideoStreamTypes.has(eventData.type)
  const shouldMirrorToAgentFlow = !(task.tool?.generateMode === 'video' && eventData.type === 'progress')
  if (shouldMirrorToAgentFlow) {
    applyBackendProgressToAgentFlow(task, eventData)
  }
  if (isVideoInternalResourceStream) {
    const isDelta = String(eventData.type || '').endsWith('_delta')
    if (!isDelta || shouldFlushStreamUi(task, 'video-internal')) {
      if (eventData.progress_msg || eventData.progressMsg || eventData.message) {
        task.progress = eventData.progress_msg || eventData.progressMsg || eventData.message
      }
      touchTask(task)
    }
    return
  }

  if (eventData.type === 'file') {
    const [file] = normalizeTaskFiles({ resources: [eventData] })
    if (file) {
      const fileId = file.resource_id || file.file_id || file.download_url
      const existing = task.files.find((item: any) => (
        String(item?.resource_id || item?.file_id || item?.download_url || '') === String(fileId || '')
      ))
      if (existing) {
        Object.assign(existing, file)
      } else {
        task.files.push(file)
      }
      if (task.tool.generateMode === 'video') {
        void maybeGeneratePresentation(task)
      }
    }
    task.updatedAt = Date.now()
    persistTasks()
    return
  }

  if (eventData.type === 'stream_start') {
    ;(task as any)._pptStream = { slides: [] }
  }

  if (eventData.type === 'stream_section_replace' && eventData.section_idx != null) {
    const stream = (task as any)._pptStream || { slides: [] }
    stream.slides = stream.slides.filter((s: any) => {
      const si = typeof s === 'object' ? s.section_idx : null
      return si !== eventData.section_idx
    })
    stream._needsRebuild = true
    ;(task as any)._pptStream = stream
  }

  if (eventData.type === 'stream_slide_start') {
    const stream = (task as any)._pptStream || { slides: [] }
    const nextSlide = {
      content: '',
      section_idx: eventData.section_idx,
      slide_idx: eventData.slide_idx,
      section_title: eventData.section_title,
      streaming: true,
    }
    const existingIdx = findPptStreamSlideIndex(stream.slides, eventData)
    if (existingIdx >= 0) {
      stream.slides[existingIdx] = nextSlide
    } else {
      stream.slides.push(nextSlide)
    }
    stream.slides = sortPptStreamSlides(stream.slides)
    ;(task as any)._pptStream = bumpPptStreamVersion(stream)
    applyStreamProgressMessage(task, eventData)
    touchTask(task)
    return
  }

  if (eventData.type === 'stream_slide_delta') {
    const stream = (task as any)._pptStream || { slides: [] }
    const existingIdx = findPptStreamSlideIndex(stream.slides, eventData)
    if (existingIdx >= 0) {
      stream.slides[existingIdx] = {
        ...stream.slides[existingIdx],
        content: `${stream.slides[existingIdx].content || ''}${eventData.delta || ''}`,
        streaming: true,
      }
    } else {
      stream.slides.push({
        content: eventData.delta || '',
        section_idx: eventData.section_idx,
        slide_idx: eventData.slide_idx,
        section_title: eventData.section_title,
        streaming: true,
      })
    }
    if (shouldFlushStreamUi(task, 'ppt')) {
      stream.slides = sortPptStreamSlides(stream.slides)
      ;(task as any)._pptStream = bumpPptStreamVersion(stream)
      applyStreamProgressMessage(task, eventData)
      touchTask(task)
    } else {
      ;(task as any)._pptStream = stream
    }
    return
  }

  if (eventData.type === 'stream_slide' && eventData.content) {
    const stream = (task as any)._pptStream || { slides: [] }
    const finalSlide = {
      content: eventData.content,
      section_idx: eventData.section_idx,
      slide_idx: eventData.slide_idx,
      section_title: eventData.section_title,
      streaming: false,
    }
    const existingIdx = findPptStreamSlideIndex(stream.slides, eventData)
    if (existingIdx >= 0) {
      stream.slides[existingIdx] = finalSlide
    } else {
      stream.slides.push(finalSlide)
    }
    stream.slides = sortPptStreamSlides(stream.slides)
    ;(task as any)._pptStream = bumpPptStreamVersion(stream)
    console.log('[GenerationTask] stream_slide taskId=%s section_idx=%s slides=%d',
      task.backendTaskId?.slice(0, 12) || task.id, eventData.section_idx, stream.slides.length)
    applyStreamProgressMessage(task, eventData)
    touchTask(task)
    return
  }

  if (eventData.type === 'stream_text_start') {
    const stream = (task as any)._textStream || { parts: [] }
    stream.file_type = eventData.file_type || stream.file_type || 'document'
    const nextPart = {
      content: '',
      section_idx: eventData.section_idx ?? 0,
      section_title: eventData.section_title || '',
      streaming: true,
    }
    const existingIdx = findTextStreamPartIndex(stream.parts, eventData)
    if (existingIdx >= 0) {
      stream.parts[existingIdx] = nextPart
    } else {
      stream.parts.push(nextPart)
    }
    stream.parts = sortTextStreamParts(stream.parts)
    ;(task as any)._textStream = stream
    task.thinkingProcess = formatTextStreamThinking(stream, task)
    applyStreamProgressMessage(task, eventData)
    touchTask(task)
    return
  }

  if (eventData.type === 'stream_text_delta') {
    const stream = (task as any)._textStream || { parts: [] }
    stream.file_type = eventData.file_type || stream.file_type || 'document'
    const existingIdx = findTextStreamPartIndex(stream.parts, eventData)
    if (existingIdx >= 0) {
      stream.parts[existingIdx] = {
        ...stream.parts[existingIdx],
        content: `${stream.parts[existingIdx].content || ''}${eventData.delta || ''}`,
        streaming: true,
      }
    } else {
      stream.parts.push({
        content: eventData.delta || '',
        section_idx: eventData.section_idx ?? 0,
        section_title: eventData.section_title || '',
        streaming: true,
      })
    }
    if (shouldFlushStreamUi(task, 'text')) {
      stream.parts = sortTextStreamParts(stream.parts)
      ;(task as any)._textStream = stream
      task.thinkingProcess = formatTextStreamThinking(stream, task)
      applyStreamProgressMessage(task, eventData)
      touchTask(task)
    } else {
      ;(task as any)._textStream = stream
    }
    return
  }

  if (eventData.type === 'stream_text_done') {
    const stream = (task as any)._textStream || { parts: [] }
    stream.file_type = eventData.file_type || stream.file_type || 'document'
    const existingIdx = findTextStreamPartIndex(stream.parts, eventData)
    const finalPart = {
      content: eventData.content || '',
      section_idx: eventData.section_idx ?? 0,
      section_title: eventData.section_title || '',
      streaming: false,
    }
    if (existingIdx >= 0) {
      stream.parts[existingIdx] = finalPart
    } else {
      stream.parts.push(finalPart)
    }
    stream.parts = sortTextStreamParts(stream.parts)
    ;(task as any)._textStream = stream
    task.thinkingProcess = formatTextStreamThinking(stream, task)
    applyStreamProgressMessage(task, eventData)
    touchTask(task)
    return
  }

  const thinking = getBackendThinking(eventData)
  if (!(task as any)._textStream) appendTaskThinking(task, thinking)

  if (eventData.progress_msg || eventData.progressMsg || eventData.progress || eventData.status) {
    task.progress = formatTaskProgress(eventData)
    task.status = toFrontendStatus(String(eventData.status || task.status || 'running'))
  }

  if (eventData.error) {
    task.error = eventData.error
    task.status = 'failed'
    task.progress = eventData.error
    finishAgentFlow(task, true)
  }

  if (eventData.result || eventData.resources) {
    task.files.splice(0, task.files.length, ...normalizeTaskFiles(eventData))
  }

  if (eventData.type === 'done' || eventData.done) {
    task.status = toFrontendStatus(String(eventData.status || 'done'))
    task.doneEvent = {
      chat_group_id: task.chatGroupId,
      resources: task.files,
      ...eventData,
    }
    if ((task as any)._textStream) {
      task.thinkingProcess = formatTextStreamThinking((task as any)._textStream, task)
    }
    finishAgentFlow(task, task.status === 'failed')
  }

  task.updatedAt = Date.now()
  persistTasks()
}

const streamBackendTask = (task: GenerationTask) => {
  if (!taskBelongsToCurrentUser(task)) return
  if (!task.backendTaskId || streamingTaskIds.has(task.backendTaskId)) return
  streamingTaskIds.add(task.backendTaskId)

  void streamResourceGenerationTask(task.backendTaskId, {
    onEvent: eventData => {
      if (!taskBelongsToCurrentUser(task)) return
      applyTaskStreamEvent(task, eventData)
    },
    onDone: async () => {
      if (!taskBelongsToCurrentUser(task)) {
        streamingTaskIds.delete(task.backendTaskId)
        return
      }
      try {
        const result = await getResourceGenerationTask(task.backendTaskId)
        applyBackendTaskData(task, unwrapResponseData(result))
      } finally {
        streamingTaskIds.delete(task.backendTaskId)
      }
    },
    onError: error => {
      if (!taskBelongsToCurrentUser(task)) return
      task.error = error || task.error
      task.updatedAt = Date.now()
      persistTasks()
    }
  }).catch(error => {
    streamingTaskIds.delete(task.backendTaskId)
    if (isBackendUnavailableError(error)) {
      task.progress = '后端暂时不可用，正在等待重连...'
      task.updatedAt = Date.now()
      persistTasks()
    }
    pollBackendTask(task)
  })
}

const runLegacyFrontendTask = (task: GenerationTask) => {
  void executeGeneration(task.text, task.tool, task.chatGroupId, {
    onSubmitted: data => {
      const submitData: any = data || {}
      task.backendTaskId = submitData?.task_id || submitData?.taskId || submitData?.id || task.backendTaskId
      task.chatGroupId = submitData?.chat_group_id || submitData?.chatGroupId || task.chatGroupId
      const thinking = getBackendThinking(submitData)
      if (thinking) {
        task.thinkingProcess = formatTaskThinking(thinking)
      }
      upsertAgentFlowNode(task, {
        type: 'agent_event',
        agent_id: 'leader',
        agent_name: 'LeaderAgent',
        phase: 'leader',
        status: 'done',
        message: '需求分析完成',
      })
      upsertAgentFlowNode(task, {
        type: 'agent_event',
        agent_id: 'executor:image',
        agent_name: RESOURCE_AGENT_LABELS.image || '图片生成智能体',
        phase: 'executor',
        status: 'running',
        message: task.progress || '正在生成图片',
        resource_type: 'image',
      })
      task.updatedAt = Date.now()
      persistTasks()
    },
    onProgress: msg => {
      task.progress = msg
      task.thinkingProcess = formatTaskThinking(msg)
      upsertAgentFlowNode(task, {
        type: 'agent_event',
        agent_id: 'executor:image',
        agent_name: RESOURCE_AGENT_LABELS.image || '图片生成智能体',
        phase: 'executor',
        status: 'running',
        message: msg,
        resource_type: 'image',
      })
      task.updatedAt = Date.now()
      persistTasks()
    },
    onThinking: msg => {
      appendTaskThinking(task, msg)
      task.updatedAt = Date.now()
      persistTasks()
    },
    onFile: fileData => {
      task.files.push(fileData)
      task.updatedAt = Date.now()
      persistTasks()
    },
    onImage: imageData => {
      task.images.push(imageData)
      upsertAgentFlowNode(task, {
        type: 'agent_event',
        agent_id: 'saver',
        agent_name: 'ResourceService',
        phase: 'saver',
        status: 'saving',
        message: '正在保存图片资源',
        resource_type: 'image',
      })
      task.updatedAt = Date.now()
      persistTasks()
    },
    onStreamStart: (eventData: any) => {
      ;(task as any)._pptStream = { slides: [] }
      task.updatedAt = Date.now()
    },
    onStreamSlide: (eventData: any) => {
      const stream = (task as any)._pptStream
      if (stream && eventData?.content) {
        stream.slides.push(eventData.content)
        task.updatedAt = Date.now()
      }
    },
    onDone: eventData => {
      task.doneEvent = eventData || null
      task.chatGroupId = (eventData as any)?.chat_group_id || (eventData as any)?.chatGroupId || task.chatGroupId
      task.status = 'done'
      if (task.tool.generateMode === 'image') {
        task.progress = '图片生成完成'
        task.thinkingProcess = formatTaskThinking('图片生成完成')
      }
      finishAgentFlow(task, false)
      task.progress = task.progress || '资源已生成'
      task.updatedAt = Date.now()
      persistTasks()
      console.log('[GenerationTask] legacy 任务完成，发送通知刷新事件', { taskId: task.backendTaskId })
      dispatchNotificationUpdateIfAway()
    },
    onError: err => {
      task.error = err || '资源生成失败，请稍后再试。'
      task.status = 'failed'
      task.progress = task.error
      finishAgentFlow(task, true)
      task.updatedAt = Date.now()
      persistTasks()
      console.log('[GenerationTask] legacy 任务失败，发送通知刷新事件', { taskId: task.backendTaskId })
      dispatchNotificationUpdateIfAway()
    },
  }).catch(error => {
    task.error = error?.message || '资源生成失败，请稍后再试。'
    task.status = 'failed'
    task.progress = task.error
    finishAgentFlow(task, true)
    task.updatedAt = Date.now()
    persistTasks()
    console.log('[GenerationTask] legacy 任务异常，发送通知刷新事件', { taskId: task.backendTaskId })
    dispatchNotificationUpdateIfAway()
  })
}

const maybeGeneratePresentation = async (task: GenerationTask) => {
  if (task.tool.generateMode !== 'video') return
  const hasSourceResource = task.files.some((file: any) => (
    file?.resourceKind !== 'presentation' &&
    file?.file_type !== 'video' &&
    file?.resource_type !== 'video'
  ))
  const canStartFromDone = task.status === 'done'
  const canStartEarly = task.status === 'running' && Boolean((task as any)._answers) && hasSourceResource
  if (!canStartFromDone && !canStartEarly) return
  if ((task.doneEvent as any)?.presentation || !hasSourceResource) return
  // 防止重复进入（watcher 可能在 files.push 时再次触发）
  if ((task as any)._presentationGenerating) return
  ;(task as any)._presentationGenerating = true

  try {
    // 新流程：追问前置已回答 → 直接生成视频，资源已按答案精准生成
    if ((task as any).questionsShown && (task as any)._answers) {
      task.status = 'running'
      task.progress = '正在生成学习视频...'
      task.updatedAt = Date.now()
      persistTasks()
      try {
        await _doGeneratePresentation(task)
      } catch (e: any) {
        task.status = 'failed'
        task.error = e?.response?.data?.detail || e?.message || '学习视频生成失败'
        task.progress = task.error
        finishAgentFlow(task, true)
      } finally {
        task.updatedAt = Date.now()
        persistTasks()
      }
      return
    }

    // 旧流程兼容：等待用户作答或无追问
    if ((task as any).pendingQuestions || (task as any).questionsShown) return

    try {
      task.status = 'running'
      task.progress = '资源已生成，正在分析内容...'
      task.updatedAt = Date.now()
      persistTasks()

      const chatGroupId = task.chatGroupId || (task.doneEvent as any)?.chat_group_id || 0
      const questionsResult: any = await getPresentationQuestions({ topic: task.text, chat_group_id: chatGroupId })
      const questions = unwrapResponseData(questionsResult)?.questions || unwrapResponseData(questionsResult)

      if (questions && Array.isArray(questions) && questions.length > 0) {
        ;(task as any).pendingQuestions = questions
        task.status = 'done'
        task.progress = '请选择视频方向以继续...'
        setAgentFlowVisibility(task, true)
      } else {
        // 无问题则直接生成
        await _doGeneratePresentation(task)
      }
    } catch (error: any) {
      // 问题生成失败 → 降级直接生成视频
      console.warn('[GenerationTask] 追问生成失败，直接生成视频:', error)
      try {
        await _doGeneratePresentation(task)
      } catch (e: any) {
        task.status = 'failed'
        task.error = e?.response?.data?.detail || e?.message || '学习视频生成失败'
        task.progress = task.error
        finishAgentFlow(task, true)
      }
    } finally {
      task.updatedAt = Date.now()
      persistTasks()
    }
  } finally {
    delete (task as any)._presentationGenerating
  }
}

const _doGeneratePresentation = async (task: GenerationTask) => {
  if (!taskBelongsToCurrentUser(task)) return
  const chatGroupId = task.chatGroupId || (task.doneEvent as any)?.chat_group_id || 0
  const answers = (task as any)._answers || undefined
  const presentationResult: any = await generatePresentation({
    topic: task.text,
    answers,
    chat_group_id: chatGroupId,
    video_mode: false,
  })
  let presentation = unwrapResponseData(presentationResult)
  const earlySourceResource: any = task.files.find((file: any) => (
    file?.resourceKind !== 'presentation' &&
    file?.file_type !== 'video' &&
    file?.resource_type !== 'video'
  )) || {}
  const earlyResourceId = earlySourceResource?.resource_id || earlySourceResource?.file_id || ''
  const earlyTitle = earlySourceResource?.topic || earlySourceResource?.title || task.text || '学习视频'
  const pendingPresentationFile = {
    ...earlySourceResource,
    file_id: presentation?.id || presentation?.presentation_id || `presentation-${earlyResourceId}`,
    presentation_id: presentation?.id || presentation?.presentation_id || '',
    source_resource_id: earlyResourceId,
    file_type: 'video',
    resource_type: 'video',
    resourceKind: 'presentation',
    filename: `${earlyTitle}.html`,
    presentation,
    preview_url: presentation?.file_url || presentation?.fileUrl || '',
    file_url: presentation?.file_url || presentation?.fileUrl || '',
    download_url: '',
    source_download_url: earlySourceResource?.download_url || earlySourceResource?.downloadUrl || (earlyResourceId ? `/resource/${earlyResourceId}/download` : ''),
  }
  task.doneEvent = { ...(task.doneEvent as object || {}), presentation, resources: [...task.files, pendingPresentationFile] }
  task.files.push(pendingPresentationFile)
  task.progress = '视频任务已提交，正在生成课件骨架...'
  task.updatedAt = Date.now()
  persistTasks()

  const presentationId = presentation?.id || presentation?.presentation_id || presentation?.presentationId
  let audioReady = false
  let resolveAudioReady: (value?: any) => void = () => {}
  const audioReadyPromise = new Promise(resolve => {
    resolveAudioReady = resolve
  })
  const markAudioReady = (eventData?: any) => {
    if (audioReady) return
    audioReady = true
    task.status = 'done'
    task.progress = '学习视频已全部制作完成，可以播放。'
    task.doneEvent = { ...(task.doneEvent as object || {}), presentation, resources: task.files }
    delete (task as any).pendingQuestions
    ;(task as any).questionsShown = true
    finishAgentFlow(task, false)
    task.updatedAt = Date.now()
    persistTasks()
    dispatchNotificationUpdateIfAway()
    resolveAudioReady(eventData || {})
  }
  const streamController = presentationId && typeof AbortController !== 'undefined'
    ? new AbortController()
    : null
  const streamPromise = presentationId
    ? streamPresentationProgress(presentationId, {
        signal: streamController?.signal,
        onEvent: eventData => {
          const payload: any = eventData || {}
          const flowStatus = String(payload.status || '').toLowerCase()
          const isSkeletonComplete = payload.type === 'agent_event' &&
            payload.phase === 'complete' &&
            flowStatus === 'done'
          const isAudioReady = payload.type === 'audio_progress' && payload.status === 'all_ready'
          const isReadyWithAudio = payload.status === 'ready' &&
            Array.isArray(payload.chapters) &&
            payload.chapters.length > 0 &&
            payload.chapters.every((chapter: any) => chapter?.is_audio_ready)

          if (isAudioReady || isReadyWithAudio) {
            markAudioReady(payload)
            return
          }

          if (isSkeletonComplete) {
            task.status = 'running'
            task.progress = '学习视频已可预览，正在补齐音频和字幕高亮...'
            task.updatedAt = Date.now()
            persistTasks()
            return
          }

          applyTaskStreamEvent(task, payload)
          if (flowStatus === 'failed') {
            task.status = 'failed'
            task.error = payload.message || '学习视频生成失败'
            task.progress = task.error
            finishAgentFlow(task, true)
            resolveAudioReady(payload)
          } else if (task.status === 'running' && payload.phase !== 'complete' && payload.message) {
            task.progress = payload.message
          }
          task.updatedAt = Date.now()
          persistTasks()
        },
        onDone: eventData => {
          const payload: any = eventData || {}
          const isAudioReady = payload.type === 'audio_progress' && payload.status === 'all_ready'
          const isReadyWithAudio = payload.status === 'ready' &&
            Array.isArray(payload.chapters) &&
            payload.chapters.length > 0 &&
            payload.chapters.every((chapter: any) => chapter?.is_audio_ready)
          if (isAudioReady || isReadyWithAudio) {
            markAudioReady(payload)
          } else {
            resolveAudioReady(payload)
          }
        },
        onError: error => {
          if (task.status === 'running' && error) {
            task.progress = String(error)
            task.updatedAt = Date.now()
            persistTasks()
          }
          resolveAudioReady({ status: 'failed', error })
        },
        doneOnAudio: true,
      }).catch(error => {
        if (error?.name === 'AbortError') return
        console.warn('[GenerationTask] 视频进度订阅失败，继续使用轮询:', error)
        resolveAudioReady({ status: 'stream_error', error })
      })
    : Promise.resolve()

  try {
    presentation = await waitForPresentationFile(presentation, message => {
      task.progress = message
      task.updatedAt = Date.now()
      persistTasks()
    })
  } catch (error) {
    streamController?.abort()
    throw error
  }

  const sourceResource: any = task.files.find((file: any) => (
    file?.resourceKind !== 'presentation' &&
    file?.file_type !== 'video' &&
    file?.resource_type !== 'video'
  )) || {}
  const resourceId = sourceResource?.resource_id || sourceResource?.file_id || ''
  const title = sourceResource?.topic || sourceResource?.title || task.text || '学习视频'

  const presentationFile = {
    ...sourceResource,
    file_id: presentation?.id || presentation?.presentation_id || `presentation-${resourceId}`,
    presentation_id: presentation?.id || presentation?.presentation_id || '',
    source_resource_id: resourceId,
    file_type: 'video',
    resource_type: 'video',
    resourceKind: 'presentation',
    filename: `${title}.html`,
    presentation,
    preview_url: presentation?.file_url || presentation?.fileUrl || '',
    file_url: presentation?.file_url || presentation?.fileUrl || '',
    download_url: '',
    source_download_url: sourceResource?.download_url || sourceResource?.downloadUrl || (resourceId ? `/resource/${resourceId}/download` : ''),
  }
  // doneEvent.presentation 必须先于 files.push 设置，
  // 否则 watcher 触发 maybeGeneratePresentation 时会因 guard 缺失而重复生成视频
  const existingPresentationFile = task.files.find((file: any) => (
    file === pendingPresentationFile ||
    file?.resourceKind === 'presentation' ||
    file?.file_type === 'video' ||
    file?.resource_type === 'video'
  ))
  if (existingPresentationFile) {
    Object.assign(existingPresentationFile, presentationFile)
  } else {
    task.files.push(presentationFile)
  }
  task.doneEvent = { ...(task.doneEvent as object || {}), presentation, resources: task.files }
  task.status = audioReady ? 'done' : 'running'
  task.progress = audioReady
    ? '学习视频已全部制作完成，可以播放。'
    : '学习视频已可预览，正在补齐音频和字幕高亮...'
  delete (task as any).pendingQuestions
  ;(task as any).questionsShown = true
  task.updatedAt = Date.now()
  persistTasks()
  if (audioReady) {
    finishAgentFlow(task, false)
    dispatchNotificationUpdateIfAway()
    streamController?.abort()
    void streamPromise
    return
  }

  await Promise.race([
    audioReadyPromise,
    wait(15 * 60 * 1000),
  ])
  if (!audioReady && task.status === 'running') {
    task.progress = '学习视频已可预览，音频仍在后台补齐中...'
    task.updatedAt = Date.now()
    persistTasks()
  }
  streamController?.abort()
  void streamPromise
}

export function useGenerationTaskQueue() {
  const _submitBackendTask = (task: GenerationTask) => {
    if (!taskBelongsToCurrentUser(task)) return
    task.status = 'running'
    task.progress = '正在提交生成任务...'
    task.updatedAt = Date.now()
    persistTasks()

    void createResourceGenerationTask({
      topic: task.text,
      resource_types: task.tool.resourceTypes || ['document'],
      chat_group_id: task.chatGroupId || 0,
      // 视频模式：中间资源不写入聊天记录，最终视频卡片才是用户想看到的
      bind_chat_history: task.tool.generateMode !== 'video',
      save_to_chat_history: task.tool.generateMode !== 'video',
      answers: (task as any)._answers || undefined,
      ppt_theme_id: task.tool.pptThemeId || undefined,
      skip_review: Boolean((task as any).skipReview || task.tool.generateMode === 'video'),
    }).then(result => {
      if (!taskBelongsToCurrentUser(task)) return
      const data = unwrapResponseData(result)
      task.backendTaskId = data?.task_id || data?.taskId || ''
      // 后端创建任务时会分配 chat_group_id，前端及时同步避免后续步骤创建新对话
      if (data?.chat_group_id || data?.chatGroupId) {
        task.chatGroupId = data?.chat_group_id || data?.chatGroupId
      }
      task.progress = formatTaskProgress(data)
      const thinking = getBackendThinking(data)
      appendTaskThinking(task, thinking)
      task.updatedAt = Date.now()
      streamBackendTask(task)
      pollBackendTask(task)
    }).catch(error => {
      if (!taskBelongsToCurrentUser(task)) return
      task.status = 'failed'
      task.error = isBackendUnavailableError(error)
        ? '后端暂时不可用，请确认后端服务或代理已启动。'
        : error?.response?.data?.detail || error?.message || '创建生成任务失败'
      task.progress = task.error
      task.updatedAt = Date.now()
    })
  }

  const startTask = (text: string, tool: ResourceToolConfig, chatGroupId: number | string | null) => {
    const ownerId = currentTaskOwnerId()
    const task: GenerationTask = reactive({
      id: makeLocalTaskId(),
      ownerUserId: ownerId,
      backendTaskId: '',
      text,
      tool,
      chatGroupId,
      status: 'running',
      progress: '正在提交生成任务...',
      thinkingProcess: '',
      error: '',
      files: [],
      images: [],
      agentFlow: createInitialAgentFlow(tool),
      doneEvent: null,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    })

    tasks.unshift(task)
    persistTasks()

    if (tool.generateMode === 'image') {
      runLegacyFrontendTask(task)
      return task
    }

    // 视频模式：追问前置，根据答案按需生成 → 减体量加速
    if (tool.generateMode === 'video') {
      task.progress = '正在分析话题...'
      persistTasks()
      void getPresentationQuestions({ topic: text, chat_group_id: chatGroupId || 0 })
        .then(async result => {
          if (!taskBelongsToCurrentUser(task)) return
          const data = unwrapResponseData(result)
          const questions = data?.questions || data
          // 后端可能为追问创建 chat_group，及时同步
          if (data?.chat_group_id || data?.chatGroupId) {
            task.chatGroupId = data?.chat_group_id || data?.chatGroupId
          }
          if (questions && Array.isArray(questions) && questions.length > 0) {
            ;(task as any).pendingQuestions = questions
            task.status = 'done'
            task.progress = '请选择视频方向以继续...'
            setAgentFlowVisibility(task, true)
          } else {
            task.status = 'running'
            task.progress = '正在生成学习视频...'
            try {
              await _doGeneratePresentation(task)
            } catch (error: any) {
              task.status = 'failed'
              task.error = error?.response?.data?.detail || error?.message || '学习视频生成失败'
              task.progress = task.error
              finishAgentFlow(task, true)
            }
          }
          persistTasks()
        })
        .catch(async () => {
          if (!taskBelongsToCurrentUser(task)) return
          task.status = 'running'
          task.progress = '正在生成学习视频...'
          task.updatedAt = Date.now()
          persistTasks()
          try {
            await _doGeneratePresentation(task)
          } catch (error: any) {
            task.status = 'failed'
            task.error = error?.response?.data?.detail || error?.message || '学习视频生成失败'
            task.progress = task.error
            finishAgentFlow(task, true)
            task.updatedAt = Date.now()
            persistTasks()
          }
        })
      return task
    }

    _submitBackendTask(task)
    return task
  }

  const answerQuestionsAndGenerate = async (task: GenerationTask, answers: Record<string, any>) => {
    if (!taskBelongsToCurrentUser(task)) return
    ;(task as any)._answers = answers
    ;(task as any).questionsShown = true
    delete (task as any).pendingQuestions

    // 新流程（追问前置）：还没有后端任务 → 先提交生成任务，答案注入 prompt 精准生成
    if (!task.backendTaskId) {
      task.status = 'running'
      task.progress = '正在生成学习视频...'
      setAgentFlowVisibility(task, true)
      task.updatedAt = Date.now()
      persistTasks()
      void _doGeneratePresentation(task).catch((error: any) => {
        task.status = 'failed'
        task.error = error?.response?.data?.detail || error?.message || '学习视频生成失败'
        task.progress = task.error
        finishAgentFlow(task, true)
      }).finally(() => {
        task.updatedAt = Date.now()
        persistTasks()
      })
      return
    }

    // 旧流程兼容：资源已生成 → 直接生成视频
    task.status = 'running'
    task.progress = '正在生成学习视频...'
    setAgentFlowVisibility(task, true)
    task.updatedAt = Date.now()
    persistTasks()

    void _doGeneratePresentation(task).catch((error: any) => {
      task.status = 'failed'
      task.error = error?.response?.data?.detail || error?.message || '学习视频生成失败'
      task.progress = task.error
      finishAgentFlow(task, true)
    }).finally(() => {
      task.updatedAt = Date.now()
      persistTasks()
    })
  }

  const hydrateTasks = async () => {
    syncTaskOwnerContext()
    const ownerSnapshot = currentTaskOwnerId()
    if (!ownerSnapshot) return []
    if (hydratePromise) return hydratePromise
    if (hasHydratedTasks) {
      tasks.filter(task => task.status === 'running' && taskBelongsToCurrentUser(task)).forEach(task => {
        streamBackendTask(task)
        pollBackendTask(task)
      })
      return tasks
    }

    hydratePromise = (async () => {
      const result = await getResourceGenerationTasks()
      if (ownerSnapshot !== currentTaskOwnerId()) return []
      const list = unwrapResponseData(result)
      const hydrated = (Array.isArray(list) ? list : [])
        .map(item => upsertBackendTask(item, undefined, ownerSnapshot))
        .filter(Boolean) as GenerationTask[]

      await Promise.all(hydrated.map(async task => {
        try {
          const detail = unwrapResponseData(await getResourceGenerationTask(task.backendTaskId))
          if (ownerSnapshot !== currentTaskOwnerId()) return
          applyBackendTaskData(task, detail)
        } catch {
          // Keep list-level task state if detail lookup is temporarily unavailable.
        }
        if (task.status === 'running') {
          streamBackendTask(task)
          pollBackendTask(task)
        }
      }))
      if (ownerSnapshot === currentTaskOwnerId()) {
        hasHydratedTasks = true
      }
      return hydrated
    })().finally(() => {
      hydratePromise = null
    })

    return hydratePromise
  }

  const getTask = (taskId: string) => tasks.find(task => taskBelongsToCurrentUser(task) && (task.id === taskId || task.backendTaskId === taskId))

  const removeTasksForConversation = (chatGroupId: number | string | null, taskIds: Array<number | string> = []) => {
    const gid = String(chatGroupId ?? '').trim()
    const idSet = new Set(taskIds.map(id => String(id || '')).filter(Boolean))
    if (!gid && !idSet.size) return

    for (let index = tasks.length - 1; index >= 0; index -= 1) {
      const task = tasks[index]
      if (!taskBelongsToCurrentUser(task)) continue
      const taskChatId = String(task.chatGroupId ?? (task as any).chat_group_id ?? '').trim()
      if ((gid && taskChatId === gid) || idSet.has(String(task.id)) || idSet.has(String(task.backendTaskId))) {
        tasks.splice(index, 1)
      }
    }
    persistTasks()
  }

  return {
    tasks,
    startTask,
    hydrateTasks,
    maybeGeneratePresentation,
    answerQuestionsAndGenerate,
    getTask,
    removeTasksForConversation,
  }
}
