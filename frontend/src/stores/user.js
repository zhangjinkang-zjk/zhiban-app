import { defineStore } from 'pinia'

export const useUserStore = defineStore('user', {
  state: () => ({
    userInfo: null,
    roleTags: []
  }),

  actions: {
    setUserInfo(data) {
      this.userInfo = data
    },

    setRoleTags(tags) {
      this.roleTags = tags
    }
  }
})