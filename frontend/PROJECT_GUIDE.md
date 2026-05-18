# 前端说明

## 技术栈

- Vue 3
- Vite
- Vue Router
- Pinia
- Axios
- Sass
- Tailwind CSS
- lucide-vue-next

## 目录结构

```txt
src
├─ api
│  ├─ apis.js
│  └─ request.js
├─ assets
│  ├─ pic
│  └─ styles
│     ├─ base-style.scss
│     └─ main.css
├─ components
│  ├─ background.vue
│  └─ NavView.vue
├─ pages
│  ├─ ChatView.vue
│  ├─ HomeView.vue
│  ├─ ResourceView.vue
│  ├─ StudyPath.vue
│  ├─ StudySituation.vue
│  └─ resouce
├─ router
│  └─ index.js
├─ stores
│  └─ user.js
├─ App.vue
└─ main.js
```

## 颜色

基础颜色集中在 `src/assets/styles/base-style.scss`。

```scss
$backgroud: #eff3f4;
$whitebc: #fafafa;

$button-green: #cdf464;
$button-black: #151a1a;

$one: #cdc3ff;
$two: #aac9ff;
$three: #98e0bb;
$four: #1d98a3;
```

## 页面功能说明

首页：有ai对话框 学习画像 学习资源 学习评分 todolist
        - ai对话框发送问题后会跳转ai对话页面
        - 学习画像首次需选择标签 每个标签点击会弹出选项 之后登陆直接用人物标签显示 空着的部分后续可以根据学习画像生成人物卡通画像
        - 学习资源 根据学习情况推荐学习资源 可点击跳转
        - 学习评分可跳转至学习情况 根据学习情况的六个维度生成评分图
        - todolist做学习计划 完整历史记录可在学习路径中展示 可跳转学习路径

ai对话
         - 点击输入框加号可选择传送图片或文件
         - 后续可添加开启新对话的按钮
         - 对话会保存 显示历史记录在右侧

学习资源
         - 有四块内容选择 可跳转至各分类页面
         - 下方可添加资源社区 用于分享学习资源

学习路径
          - 展示已学习资源
          - 推荐学习资源
          - 剩下待补充

学习情况
          - 展示学生标签 可进行补充修改
          - 展示评分大图 对每项进行详细说明
          - 展示学习时长
          - 展示ai提升建议
