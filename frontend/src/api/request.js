import axios from 'axios'

const request = axios.create({
  baseURL: 'http://119.45.14.154:777/',
  timeout: 60000
})

request.interceptors.request.use(
  config => {
    const token = localStorage.getItem('token')
    const publicUrls = ['/user/create_user', '/user/login_user']

    if (token && !publicUrls.includes(config.url)) {
      config.headers.token = token
    }

    return config
  },
  error => {
    return Promise.reject(error)
  }
)

//响应拦截器：处理token过期
request.interceptors.response.use(
  response => {
    return response.data
  },
  error => {
    if (error.response && error.response.status === 401) {
      localStorage.removeItem('token')


      window.location.href = '/LoginView'

      console.error('登录已过期，请重新登录')
    }
    return Promise.reject(error)
  }
)

export default request
