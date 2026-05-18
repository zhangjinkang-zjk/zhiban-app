<template>
  <main class="home-cover">
    <FloatingHomeNav />

    <router-link class="account-corner" to="/profile" :aria-label="isLoggedIn ? '个人信息' : '登录'">
      <span class="avatar">{{ avatarText }}</span>
      <span class="account-text">
        <strong>{{ accountName }}</strong>
        <small>{{ accountRole }}</small>
      </span>
    </router-link>

    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Personal Learning Companion</p>
        <h1>知伴</h1>
        <p class="lead">
          汇集 AI 对话、学习画像、资料导入与路径规划，让你的学习状态、资源和计划放在同一个清晰界面里。
        </p>

        <div class="hero-actions">
          <router-link class="primary-action" to="/chat">开始 AI 对话</router-link>
          <router-link class="secondary-action" to="/study-import">导入学习资料</router-link>
        </div>
      </div>

      <div class="hero-board" aria-label="首页功能预览">
        <article class="feature-card main-card">
          <div class="feature-head">
            <img src="../assets/pic/message.svg" alt="" />
            <span>AI 对话</span>
          </div>
          <p>根据你的问题、资料和画像生成学习建议。</p>
          <div class="chat-preview">
            <span></span>
            <span></span>
            <span></span>
          </div>
        </article>

        <article class="feature-card">
          <div class="feature-head">
            <img src="../assets/pic/yonghuhuaxiang.svg" alt="" />
            <span>学习画像</span>
          </div>
          <div class="tag-row">
            <span>兴趣</span>
            <span>方式</span>
            <span>能力</span>
          </div>
        </article>

        <article class="feature-card wide-card">
          <div class="feature-head">
            <img src="../assets/pic/todolist.svg" alt="" />
            <span>学习路径</span>
          </div>
          <div class="progress-list">
            <span></span>
            <span></span>
            <span></span>
          </div>
        </article>

        <router-link class="mini-import-btn" to="/study-import">
          资料导入
        </router-link>
      </div>
    </section>

    <LoginView
      :visible="showLogin"
      @close="showLogin = false"
      @login="handleLoginSuccess"
    />
  </main>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getUserProfile } from '../api/apis'
import FloatingHomeNav from '../components/FloatingHomeNav.vue'
import LoginView from '../components/LoginView.vue'

const router = useRouter()
const token = ref(localStorage.getItem('token') || '')
const username = ref(localStorage.getItem('username') || '')
const userRole = ref(localStorage.getItem('role') || localStorage.getItem('identity') || '')
const showLogin = ref(false)

const isLoggedIn = computed(() => Boolean(token.value))
const accountName = computed(() => (isLoggedIn.value ? username.value.trim() || '正在获取用户' : '请登录账户'))
const accountRole = computed(() => {
  if (!isLoggedIn.value) return '点击进入个人信息'
  if (userRole.value === 'teacher') return '教师'
  if (userRole.value === 'student') return '学生'
  return '学生'
})
const avatarText = computed(() => {
  return isLoggedIn.value ? accountName.value.slice(0, 1).toUpperCase() : '登'
})

const normalizeProfile = (result) => {
  return result?.data || result?.user || result || {}
}

const loadAccountInfo = async () => {
  if (!token.value) return

  try {
    const profile = normalizeProfile(await getUserProfile())

    if (profile.username) {
      username.value = profile.username
      localStorage.setItem('username', profile.username)
    }

    if (profile.role || profile.identity) {
      userRole.value = profile.role || profile.identity
      localStorage.setItem('role', userRole.value)
    }
  } catch (error) {
    username.value = localStorage.getItem('username') || '已登录账户'
  }
}

const openLogin = () => {
  showLogin.value = true
}

const handleLoginSuccess = async () => {
  token.value = localStorage.getItem('token') || ''
  showLogin.value = false
  await loadAccountInfo()
  await router.push('/')
}

const handleAccountClick = event => {
  if (token.value) return

  const accountEntry = event.target.closest?.('.account-corner')

  if (!accountEntry) return

  event.preventDefault()
  openLogin()
}

onMounted(() => {
  loadAccountInfo()
  document.addEventListener('click', handleAccountClick, true)
})

onUnmounted(() => {
  document.removeEventListener('click', handleAccountClick, true)
})
</script>

<style scoped>
.home-cover {
  min-height: 100vh;
  background:
    linear-gradient(135deg, rgba(240, 239, 221, 0.78) 0%, rgba(250, 250, 250, 0.92) 42%, rgba(201, 220, 233, 0.72) 100%),
    #fafafa;
  color: #163f8f;
  font-family:
    Inter,
    -apple-system,
    BlinkMacSystemFont,
    "Segoe UI",
    "PingFang SC",
    "Microsoft YaHei",
    sans-serif;
  overflow: hidden;
}

.primary-action,
.secondary-action,
.mini-import-btn {
  text-decoration: none;
}

.account-corner {
  position: fixed;
  top: 24px;
  right: 28px;
  z-index: 30;
  min-height: 56px;
  padding: 7px 12px 7px 7px;
  border-radius: 999px;
  border: 1px solid rgba(201, 220, 233, 0.82);
  background: rgba(250, 250, 250, 0.72);
  color: #163f8f;
  text-decoration: none;
  backdrop-filter: blur(18px) saturate(135%);
  -webkit-backdrop-filter: blur(18px) saturate(135%);
  display: inline-flex;
  align-items: center;
  gap: 10px;
  box-shadow:
    0 14px 34px rgba(22, 63, 143, 0.14),
    inset 0 1px 0 rgba(250, 250, 250, 0.76);
  transition:
    transform 0.22s ease,
    box-shadow 0.22s ease,
    background 0.22s ease;
}

.account-corner:hover {
  background: rgba(250, 250, 250, 0.86);
  transform: translateY(-2px);
  box-shadow:
    0 18px 40px rgba(22, 63, 143, 0.18),
    inset 0 1px 0 rgba(250, 250, 250, 0.8);
}

.account-corner:active {
  transform: translateY(0) scale(0.98);
}

.avatar {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  background: #163f8f;
  color: #fafafa;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  flex-shrink: 0;
}

.account-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.15;
  white-space: nowrap;
}

.account-text strong {
  color: #163f8f;
  font-size: 14px;
}

.account-text small {
  color: #5f8fc3;
  font-size: 12px;
  font-weight: 700;
}

.hero {
  min-height: 100vh;
  padding: 128px 7vw 54px;
  display: grid;
  grid-template-columns: minmax(320px, 0.95fr) minmax(420px, 1.05fr);
  gap: 46px;
  align-items: center;
}

.hero-copy {
  max-width: 560px;
}

.eyebrow {
  margin: 0 0 14px;
  color: #5f8fc3;
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 0;
}

.hero h1 {
  margin: 0;
  color: #163f8f;
  font-size: clamp(64px, 9vw, 118px);
  line-height: 0.92;
  letter-spacing: 0;
}

.lead {
  margin: 24px 0 0;
  color: #5f8fc3;
  font-size: 18px;
  line-height: 1.8;
}

.hero-actions {
  margin-top: 34px;
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.primary-action,
.secondary-action {
  position: relative;
  overflow: hidden;
  height: 46px;
  padding: 0 22px;
  border-radius: 999px;
  font-size: 15px;
  font-weight: 800;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition:
    transform 0.22s ease,
    box-shadow 0.22s ease,
    background 0.22s ease,
    border-color 0.22s ease;
}

.primary-action::after,
.secondary-action::after,
.mini-import-btn::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(110deg, transparent 0%, rgba(250, 250, 250, 0.46) 45%, transparent 70%);
  transform: translateX(-120%);
  transition: transform 0.55s ease;
}

.primary-action {
  background: #163f8f;
  color: #fafafa;
  border: 1px solid #163f8f;
}

.secondary-action {
  background: #fafafa;
  color: #163f8f;
  border: 1px solid #c9dce9;
}

.primary-action:hover,
.secondary-action:hover,
.mini-import-btn:hover {
  transform: translateY(-3px);
  box-shadow: 0 14px 28px rgba(22, 63, 143, 0.16);
}

.primary-action:hover::after,
.secondary-action:hover::after,
.mini-import-btn:hover::after {
  transform: translateX(120%);
}

.primary-action:hover {
  background: #5f8fc3;
  border-color: #5f8fc3;
}

.secondary-action:hover,
.mini-import-btn:hover {
  background: #c9dce9;
  border-color: #5f8fc3;
}

.primary-action:active,
.secondary-action:active,
.mini-import-btn:active {
  transform: translateY(0) scale(0.98);
  box-shadow: none;
}

.hero-board {
  min-height: 520px;
  padding: 18px;
  border: 1px solid #c9dce9;
  border-radius: 24px;
  background:rgba(240,239,221,0.05) ;
  display: grid;
  grid-template-columns: 1.15fr 0.85fr;
  grid-template-rows: 1fr 1fr auto;
  gap: 14px;
  box-shadow: 0 24px 56px rgba(22, 63, 143, 0.14);
}

.feature-card {
  padding: 18px;
  border-radius: 20px;
  border: 1px solid #c9dce9;
  background: #fafafa;
  min-width: 0;
  transition:
    transform 0.22s ease,
    box-shadow 0.22s ease,
    border-color 0.22s ease;
}

.feature-card:hover {
  transform: translateY(-4px);
  border-color: #5f8fc3;
  box-shadow: 0 16px 34px rgba(22, 63, 143, 0.13);
}

.main-card {
  grid-row: span 2;
  background: #c9dce9;
}

.wide-card {
  background: #163f8f;
  color: #fafafa;
}

.mini-import-btn {
  position: relative;
  overflow: hidden;
  min-height: 38px;
  padding: 0 14px;
  border-radius: 999px;
  border: 1px solid #c9dce9;
  background: #fafafa;
  color: #163f8f;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  justify-self: end;
  font-size: 13px;
  font-weight: 800;
  transition:
    transform 0.22s ease,
    box-shadow 0.22s ease,
    background 0.22s ease;
}

.mini-import-btn:hover {
  background: #c9dce9;
}

.feature-head {
  display: flex;
  align-items: center;
  gap: 9px;
  color: inherit;
  font-size: 16px;
  font-weight: 800;
}

.feature-head img {
  width: 22px;
  height: 22px;
}

.feature-card p {
  margin: 14px 0 0;
  color: #5f8fc3;
  font-size: 14px;
  line-height: 1.7;
}

.main-card p {
  color: #163f8f;
}

.chat-preview {
  margin-top: 40px;
  display: grid;
  gap: 14px;
}

.chat-preview span,
.progress-list span {
  height: 18px;
  border-radius: 999px;
  background: #fafafa;
}

.chat-preview span:nth-child(2) {
  width: 78%;
  background: #f0efdd;
}

.chat-preview span:nth-child(3) {
  width: 58%;
}

.tag-row {
  margin-top: 24px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.tag-row span {
  height: 30px;
  padding: 0 12px;
  border-radius: 999px;
  background: #f0efdd;
  color: #163f8f;
  display: inline-flex;
  align-items: center;
  font-size: 13px;
  font-weight: 700;
}

.wide-card .feature-head {
  color: #fafafa;
}

.progress-list {
  margin-top: 26px;
  display: grid;
  gap: 12px;
}

.progress-list span {
  background: #c9dce9;
}

.progress-list span:nth-child(2) {
  width: 72%;
}

.progress-list span:nth-child(3) {
  width: 48%;
}

@media (max-width: 980px) {
  .hero {
    grid-template-columns: 1fr;
    padding-top: 154px;
  }
}

@media (max-width: 640px) {
  .account-corner {
    top: 14px;
    right: 16px;
  }

  .account-text {
    display: none;
  }

  .hero {
    padding: 150px 20px 30px;
  }

  .lead {
    font-size: 16px;
  }

  .hero-board {
    min-height: auto;
    grid-template-columns: 1fr;
  }
}
</style>
