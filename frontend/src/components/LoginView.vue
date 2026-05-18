<template>
  
  <transition name="fade">
    <div v-if="visible" class="login-mask" @click.self="closeModal">
      <transition name="pop">
        <div class="login-modal">
          <button class="close-btn" @click="closeModal">×</button>

          <div class="login-layout">
    <div class="monster-panel">
      <AnimatedCharacters
        :is-typing="isTyping"
        :show-password="showPassword"
        :password-length="form.password.length"
      />
    </div>


          <div class="login-content">
            <h2>{{ isRegister ? '创建账号' : '欢迎回来' }}</h2>
            <p class="subtitle">
              {{ isRegister ? '注册知伴账号，开始你的学习旅程' : '登录你的知伴账号，继续学习旅程' }}
            </p>

            <form @submit.prevent="handleSubmit">
              <div class="form-item">
                <label>用户名 / 邮箱</label>
                <input
                  v-model="form.account"
                  type="text"
                  autocomplete="username"
                  placeholder="请输入用户名或邮箱"
                  @focus="isTyping = true"
                  @blur="isTyping = false"
                />
              </div>

              <div class="form-item">
                <label>密码</label>
                <div class="password-box">
                  <input
                    v-model="form.password"
                    :type="showPassword ? 'text' : 'password'"
                    :autocomplete="isRegister ? 'new-password' : 'current-password'"
                    placeholder="请输入密码"
                    @focus="isTyping = true"
                    @blur="isTyping = false"
                  />
                  <span @click="showPassword = !showPassword">
                    {{ showPassword ? '隐藏' : '显示' }}
                  </span>
                </div>
              </div>

              <p v-if="errorMessage" class="error-message">{{ errorMessage }}</p>
              <p v-if="successMessage" class="success-message">{{ successMessage }}</p>

              <button class="login-btn" type="submit" :disabled="loading">
                {{ buttonText }}
              </button>
            </form>

            <p class="register-text">
              {{ isRegister ? '已有账号？' : '还没有账号？' }}
              <span @click="toggleMode">{{ isRegister ? '去登录' : '立即注册' }}</span>
            </p>
          </div>
        </div>
        </div>
      </transition>
    </div>
  </transition>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { login, register } from '../api/apis'
import AnimatedCharacters from './AnimatedCharacters.vue'; 
defineProps({
  visible: {
    type: Boolean,
    default: false
  }
})

const isTyping = ref(false);

const emit = defineEmits(['close', 'login', 'register'])

const isRegister = ref(false)
const showPassword = ref(false)
const loading = ref(false)
const errorMessage = ref('')
const successMessage = ref('')

const form = reactive({
  account: '',
  password: ''
})

const buttonText = computed(() => {
  if (loading.value) return isRegister.value ? '注册中...' : '登录中...'
  return isRegister.value ? '注册' : '登录'
})

const resetMessage = () => {
  errorMessage.value = ''
  successMessage.value = ''
}

const closeModal = () => {
  if (loading.value) return
  emit('close')
}

const toggleMode = () => {
  if (loading.value) return
  isRegister.value = !isRegister.value
  resetMessage()
}

const validateForm = () => {
  if (!form.account.trim() || !form.password.trim()) {
    errorMessage.value = '请输入用户名/邮箱和密码'
    return false
  }

  if (isRegister.value && form.password.length < 6) {
    errorMessage.value = '密码至少需要 6 位'
    return false
  }

  return true
}

const buildLoginPayload = () => {
  const account = form.account.trim()

  return {
    [account.includes('@') ? 'email' : 'username']: account,
    password: form.password
  }
}

const saveLoginUser = (result) => {
  const user = result?.data || result?.user || result

  if (user?.token) {
    localStorage.setItem('token', user.token)
  }

  if (result?.token) {
    localStorage.setItem('token', result.token)
  }

  if (user?.id) {
    localStorage.setItem('token', String(user.id))
  }

  if (user?.id) {
    localStorage.setItem('user_id', String(user.id))
  }
}

const checkResponse = (result) => {
  if (result?.code && result.code !== 200) {
    throw new Error(result.msg || '请求失败')
  }
}

const handleSubmit = async () => {
  resetMessage()

  if (!validateForm()) return

  loading.value = true

  try {
    if (isRegister.value) {
      const result = await register({
        username: form.account.trim(),
        password: form.password
      })

      checkResponse(result)
      emit('register', result)
      successMessage.value = '注册成功，请登录'
      isRegister.value = false
      form.password = ''
      return
    }

    const result = await login(buildLoginPayload())
    checkResponse(result)
    saveLoginUser(result)
    emit('login', result)
    emit('close')
  } catch (error) {
    errorMessage.value =
      error?.response?.data?.detail ||
      error?.response?.data?.msg ||
      error?.response?.data?.message ||
      error?.message ||
      (isRegister.value ? '注册失败，请稍后重试' : '登录失败，请稍后重试')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-mask {
  position: fixed;
  inset: 0;
  background: rgba(17, 24, 39, 0.35);
  backdrop-filter: blur(6px);
  display: flex;
  justify-content: center;
  align-items: center;
  z-index: 99;
}

.login-modal {
  position: relative;
  width: min(900px, calc(100vw - 32px));
  background: #fafafa;
  border-radius: 22px;
  overflow: hidden;
  box-shadow: 0 24px 70px rgba(24, 63, 143, 0.22);
  border: 1px solid rgba(215, 228, 239, 0.9);
}

.login-layout {
  min-height: 520px;
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(360px, 0.92fr);
}

.monster-panel {
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding: 44px 20px 0;
  background:
    linear-gradient(180deg, #f4f3df 0%, #d7e4ef 68%, #638fc2 100%);
  overflow: hidden;
}

.monster-panel :deep(.monster-stage) {
  transform: scale(0.74);
  transform-origin: bottom center;
}

.close-btn {
  position: absolute;
  right: 18px;
  top: 16px;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 50%;
  background: rgba(250, 250, 250, 0.82);
  color: #183f8f;
  font-size: 22px;
  line-height: 30px;
  cursor: pointer;
  z-index: 2;
  transition: all 0.2s ease;
}

.close-btn:hover {
  background: #183f8f;
  color: #fafafa;
  transform: rotate(90deg);
}

.login-content {
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 48px 48px 44px;
}

.login-content h2 {
  margin: 0;
  color: #183f8f;
  font-size: 28px;
  font-weight: 700;
  letter-spacing: 1px;
}

.subtitle {
  margin: 8px 0 28px;
  color: #638fc2;
  font-size: 14px;
}

.form-item {
  margin-bottom: 18px;
}

.form-item label {
  display: block;
  margin-bottom: 8px;
  color: #183f8f;
  font-size: 14px;
  font-weight: 600;
}

.form-item input {
  width: 100%;
  height: 46px;
  box-sizing: border-box;
  border: 1px solid #d7e4ef;
  border-radius: 12px;
  padding: 0 14px;
  background: #fafafa;
  color: #183f8f;
  font-size: 14px;
  outline: none;
  transition: all 0.25s ease;
}

.form-item input:focus {
  border-color: #638fc2;
  box-shadow: 0 0 0 4px rgba(99, 143, 194, 0.18);
}

.password-box {
  position: relative;
}

.password-box input {
  padding-right: 56px;
}

.password-box span {
  position: absolute;
  right: 14px;
  top: 50%;
  transform: translateY(-50%);
  color: #183f8f;
  font-size: 13px;
  cursor: pointer;
  user-select: none;
}

.password-box span:hover {
  color: #638fc2;
}

.error-message,
.success-message {
  margin: -8px 0 14px;
  font-size: 13px;
  line-height: 1.5;
}

.error-message {
  color: #c2410c;
}

.success-message {
  color: #15803d;
}

.login-btn {
  width: 100%;
  height: 48px;
  border: none;
  border-radius: 14px;
  background: #183f8f;
  color: #fafafa;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 2px;
  cursor: pointer;
  box-shadow: 0 12px 24px rgba(24, 63, 143, 0.25);
  transition: all 0.25s ease;
}

.login-btn:hover:not(:disabled) {
  background: #638fc2;
  transform: translateY(-2px);
  box-shadow: 0 16px 32px rgba(24, 63, 143, 0.32);
}

.login-btn:disabled {
  cursor: not-allowed;
  opacity: 0.7;
}

.register-text {
  margin: 24px 0 0;
  text-align: center;
  font-size: 14px;
  color: #638fc2;
}

.register-text span {
  color: #183f8f;
  font-weight: 600;
  cursor: pointer;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.25s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.pop-enter-active,
.pop-leave-active {
  transition: all 0.25s ease;
}

.pop-enter-from,
.pop-leave-to {
  opacity: 0;
  transform: scale(0.92) translateY(12px);
}

@media (max-width: 760px) {
  .login-modal {
    width: calc(100% - 32px);
  }

  .login-layout {
    grid-template-columns: 1fr;
  }

  .monster-panel {
    min-height: 220px;
    padding-top: 24px;
  }

  .monster-panel :deep(.monster-stage) {
    transform: scale(0.48);
  }

  .login-content {
    padding: 30px 24px 32px;
  }
}
</style>
