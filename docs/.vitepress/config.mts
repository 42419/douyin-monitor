import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  title: 'dywatch',
  description: '抖音账号视频监控 —— 基于 Douyin_TikTok_Download_API v5，默认只读',
  lastUpdated: true,
  cleanUrls: true,

  head: [
    ['link', { rel: 'icon', type: 'image/png', href: '/favicon.png' }],
  ],

  themeConfig: {
    logo: '/logo.svg',

    nav: [
      { text: '指南', link: '/guide/what-is-dywatch' },
      { text: '配置', link: '/config/reference' },
      { text: '运维', link: '/operations/upgrade' },
      {
        text: '相关链接',
        items: [
          { text: 'dywatch (GitHub)', link: 'https://github.com/42419/douyin-monitor' },
          { text: 'Douyin_TikTok_Download_API (GitHub)', link: 'https://github.com/Evil0ctal/Douyin_TikTok_Download_API' },
        ],
      },
    ],

    sidebar: {
      '/guide/': [
        {
          text: '介绍',
          items: [
            { text: '这是什么', link: '/guide/what-is-dywatch' },
            { text: '架构总览', link: '/guide/architecture' },
          ],
        },
        {
          text: '快速开始',
          items: [
            { text: '前置：DTK v5 与 API Key', link: '/guide/dtk-setup' },
            { text: '安装 dywatch', link: '/guide/quick-start' },
            { text: '命令行', link: '/guide/commands' },
            { text: '监控列表 users.conf', link: '/guide/users-conf' },
            { text: '只读面板', link: '/guide/dashboard' },
          ],
        },
        {
          text: '工作原理',
          items: [
            { text: '判定规则', link: '/guide/detection-rules' },
            { text: '归档下载', link: '/guide/archive-download' },
          ],
        },
      ],
      '/config/': [
        {
          text: '配置',
          items: [
            { text: '配置参考', link: '/config/reference' },
            { text: '容量估算', link: '/config/capacity' },
          ],
        },
      ],
      '/operations/': [
        {
          text: '运维',
          items: [
            { text: '升级', link: '/operations/upgrade' },
            { text: '日志与轮转', link: '/operations/logging' },
            { text: '排障', link: '/operations/troubleshooting' },
          ],
        },
      ],
      '/reference/': [
        {
          text: '参考',
          items: [
            { text: '事件类型', link: '/reference/events' },
            { text: '权限 / API Key', link: '/reference/permissions' },
          ],
        },
      ],
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/42419/douyin-monitor' },
    ],

    footer: {
      message: '基于 MIT 协议发布',
      copyright: 'dywatch —— 建立在 Douyin_TikTok_Download_API v5 之上',
    },

    search: {
      provider: 'local',
    },

    outline: {
      label: '本页目录',
    },

    docFooter: {
      prev: '上一篇',
      next: '下一篇',
    },

    returnToTopLabel: '回到顶部',
    sidebarMenuLabel: '菜单',
    darkModeSwitchLabel: '主题',
    lastUpdatedText: '最后更新',
  },
})
