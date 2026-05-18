<template>
  <div v-if="showPortraitSetup" class="portrait-mask">
    <section class="portrait-panel">
      <header class="portrait-header">
        <div>
          <p class="portrait-kicker">学生画像初始化</p>
          <h2>先选几个标签，让知伴更懂你</h2>
          <span>每个维度选择 1 到 3 个标签，点击标签后会出现在右侧表单中。</span>
        </div>
      </header>

      <div class="portrait-layout">
        <div class="portrait-grid">
          <article
            v-for="dimension in portraitDimensions"
            :key="dimension.key"
            class="portrait-card"
          >
            <div class="portrait-card-head">
              <div>
                <h3>{{ dimension.title }}</h3>
                <p>{{ dimension.description }}</p>
              </div>
              <strong>{{ selectedPortraitTags[dimension.key].length }}/3</strong>
            </div>

            <div class="tag-list">
              <button
                v-for="tag in dimension.tags"
                :key="tag"
                type="button"
                class="tag-chip"
                :class="{ selected: isPortraitTagSelected(dimension.key, tag) }"
                @click="togglePortraitTag(dimension.key, tag)"
              >
                {{ tag }}
              </button>
            </div>
          </article>
        </div>

        <aside v-if="hasAnyPortraitTag" class="portrait-form-panel">
          <h3>已选择标签</h3>
          <div
            v-for="dimension in portraitDimensions"
            :key="dimension.key"
            class="selected-group"
          >
            <label>{{ dimension.title }}</label>
            <div class="selected-tags">
              <span
                v-for="tag in selectedPortraitTags[dimension.key]"
                :key="tag"
              >
                {{ tag }}
              </span>
              <em v-if="selectedPortraitTags[dimension.key].length === 0">至少选择 1 个</em>
            </div>
          </div>

          <p v-if="portraitError" class="portrait-error">{{ portraitError }}</p>

          <button
            class="portrait-submit"
            type="button"
            :disabled="portraitSaving"
            @click="submitPortraitSetup"
          >
            {{ portraitSaving ? '保存中...' : '完成初始化' }}
          </button>
        </aside>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { getPortrait, initPortrait } from '../api/apis'

const showPortraitSetup = ref(false)
const portraitSaving = ref(false)
const portraitError = ref('')

const portraitDimensions = [
  {
    key: 'interests',
    title: '兴趣爱好',
    description: '了解学生的兴趣方向，便于资源推荐',
    tags: ['编程', '科学探索', '绘画', '音乐', '体育', '阅读', '社会实践', '手工', '游戏策略', '摄影']
  },
  {
    key: 'learningStyle',
    title: '学习方式',
    description: '了解学生学习习惯和偏好',
    tags: ['独立学习', '小组讨论', '动手操作', '多媒体学习', '喜欢记笔记', '喜欢问问题', '快速学习', '深度思考', '喜欢做练习']
  },
  {
    key: 'personality',
    title: '性格特征',
    description: '帮助分析沟通、创造力和适应力',
    tags: ['外向', '内向', '乐观', '谨慎', '喜欢挑战', '稳妥', '主动', '被动', '细心', '粗心']
  },
  {
    key: 'subjects',
    title: '学科偏好',
    description: '学科兴趣和优势领域',
    tags: ['数学', '物理', '化学', '生物', '语文', '英语', '历史', '地理', '艺术', '体育']
  },
  {
    key: 'skills',
    title: '技能能力',
    description: '硬技能和软技能，便于画像分析',
    tags: ['逻辑思维', '语言表达', '动手能力', '团队协作', '创意设计', '分析判断', '编程技能', '领导力', '时间管理']
  },
  {
    key: 'values',
    title: '价值观与行为习惯',
    description: '学生的态度与行为特征',
    tags: ['守时', '勤奋', '好奇', '探索', '责任感', '独立', '合作', '自律', '适应力', '主动性']
  }
]

const selectedPortraitTags = ref(
  portraitDimensions.reduce((result, item) => {
    result[item.key] = []
    return result
  }, {})
)

const hasAnyPortraitTag = computed(() => {
  return portraitDimensions.some(item => selectedPortraitTags.value[item.key].length > 0)
})

const isPortraitComplete = computed(() => {
  return portraitDimensions.every(item => {
    const count = selectedPortraitTags.value[item.key].length
    return count >= 1 && count <= 3
  })
})

const getResponseData = (res) => {
  return res?.data ?? res ?? {}
}

const isPortraitTagSelected = (dimensionKey, tag) => {
  return selectedPortraitTags.value[dimensionKey].includes(tag)
}

const togglePortraitTag = (dimensionKey, tag) => {
  portraitError.value = ''

  const currentTags = selectedPortraitTags.value[dimensionKey]
  const index = currentTags.indexOf(tag)

  if (index >= 0) {
    currentTags.splice(index, 1)
    return
  }

  if (currentTags.length >= 3) {
    portraitError.value = '每个维度最多选择 3 个标签'
    return
  }

  currentTags.push(tag)
}

const hasSavedPortraitTags = (portrait) => {
  const tags = portrait?.personality_tags

  if (!tags) return false

  try {
    const parsed = typeof tags === 'string' ? JSON.parse(tags) : tags

    if (Array.isArray(parsed)) {
      return parsed.length > 0
    }

    if (parsed && typeof parsed === 'object') {
      return portraitDimensions.some(item => Array.isArray(parsed[item.key]) && parsed[item.key].length > 0)
    }
  } catch (error) {
    return String(tags).trim().length > 0
  }

  return false
}

const checkPortraitSetup = async () => {
  if (!localStorage.getItem('token')) return

  try {
    const res = await getPortrait()
    const portrait = getResponseData(res)
    showPortraitSetup.value = !hasSavedPortraitTags(portrait)
  } catch (error) {
    showPortraitSetup.value = true
  }
}

const submitPortraitSetup = async () => {
  if (!isPortraitComplete.value) {
    portraitError.value = '请在每个维度选择 1 到 3 个标签'
    return
  }

  portraitSaving.value = true
  portraitError.value = ''

  const tagsPayload = portraitDimensions.flatMap(item => selectedPortraitTags.value[item.key])

  try {
    const res = await initPortrait({
      cognition: null,
      learning_goal: null,
      personality_tags: JSON.stringify(tagsPayload)
    })

    const data = res || {}
    if (data.code && data.code !== 200) {
      throw new Error(data.msg || '画像初始化失败')
    }

    showPortraitSetup.value = false
  } catch (error) {
    portraitError.value =
      error?.response?.data?.detail ||
      error?.response?.data?.msg ||
      error?.message ||
      '保存失败，请稍后再试'
  } finally {
    portraitSaving.value = false
  }
}

onMounted(checkPortraitSetup)
</script>

<style scoped>
.portrait-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  padding: 28px;
  background: rgba(17, 24, 39, 0.38);
  display: flex;
  align-items: center;
  justify-content: center;
}

.portrait-panel {
  width: min(1180px, 100%);
  max-height: calc(100vh - 56px);
  overflow: hidden;
  border-radius: 18px;
  background: #fafafa;
  border: 1px solid rgba(201, 220, 233, 0.95);
  box-shadow: 0 28px 72px rgba(15, 23, 42, 0.22);
  display: flex;
  flex-direction: column;
}

.portrait-header {
  padding: 24px 28px 20px;
  border-bottom: 1px solid rgba(201, 220, 233, 0.9);
  background: #f0efdd;
}

.portrait-kicker {
  margin: 0 0 8px;
  color: #5f8fc3;
  font-size: 13px;
  font-weight: 700;
}

.portrait-header h2 {
  margin: 0;
  color: #163f8f;
  font-size: 24px;
  line-height: 1.3;
}

.portrait-header span {
  display: block;
  margin-top: 8px;
  color: #475569;
  font-size: 14px;
}

.portrait-layout {
  min-height: 0;
  display: grid;
  grid-template-columns: 1fr 300px;
}

.portrait-grid {
  min-height: 0;
  max-height: calc(100vh - 178px);
  overflow-y: auto;
  padding: 22px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.portrait-card {
  padding: 16px;
  border: 1px solid rgba(201, 220, 233, 0.95);
  border-radius: 8px;
  background: #ffffff;
}

.portrait-card-head {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  align-items: flex-start;
  margin-bottom: 12px;
}

.portrait-card h3,
.portrait-form-panel h3 {
  margin: 0;
  color: #111827;
  font-size: 16px;
}

.portrait-card p {
  margin: 6px 0 0;
  color: #64748b;
  font-size: 12px;
  line-height: 1.5;
}

.portrait-card strong {
  min-width: 38px;
  height: 24px;
  border-radius: 999px;
  background: #c9dce9;
  color: #163f8f;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.tag-chip {
  min-height: 32px;
  padding: 0 11px;
  border: 1px solid #c9dce9;
  border-radius: 999px;
  background: #fafafa;
  color: #334155;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.18s ease;
}

.tag-chip:hover {
  border-color: #5f8fc3;
  color: #163f8f;
}

.tag-chip.selected {
  background: #163f8f;
  border-color: #163f8f;
  color: #fafafa;
}

.portrait-form-panel {
  max-height: calc(100vh - 178px);
  overflow-y: auto;
  padding: 22px;
  border-left: 1px solid rgba(201, 220, 233, 0.9);
  background: #f8fafc;
}

.selected-group {
  margin-top: 16px;
}

.selected-group label {
  display: block;
  margin-bottom: 8px;
  color: #5f8fc3;
  font-size: 13px;
  font-weight: 700;
}

.selected-tags {
  min-height: 38px;
  padding: 8px;
  border: 1px solid #c9dce9;
  border-radius: 8px;
  background: #ffffff;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.selected-tags span {
  padding: 5px 8px;
  border-radius: 999px;
  background: #c9dce9;
  color: #163f8f;
  font-size: 12px;
}

.selected-tags em {
  color: #94a3b8;
  font-size: 12px;
  font-style: normal;
  align-self: center;
}

.portrait-error {
  margin: 16px 0 0;
  color: #b91c1c;
  font-size: 13px;
}

.portrait-submit {
  width: 100%;
  height: 42px;
  margin-top: 18px;
  border: none;
  border-radius: 10px;
  background: #163f8f;
  color: #fafafa;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
}

.portrait-submit:disabled {
  background: #5f8fc3;
  cursor: not-allowed;
}

@media (max-width: 900px) {
  .portrait-layout {
    grid-template-columns: 1fr;
  }

  .portrait-grid {
    grid-template-columns: 1fr;
  }

  .portrait-form-panel {
    border-left: none;
    border-top: 1px solid rgba(201, 220, 233, 0.9);
  }
}

@media (max-width: 700px) {
  .portrait-mask {
    padding: 12px;
  }

  .portrait-header {
    padding: 20px;
  }

  .portrait-grid,
  .portrait-form-panel {
    padding: 14px;
  }
}
</style>
