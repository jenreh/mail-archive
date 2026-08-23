import { defineConfig } from 'vitepress'

// https://vitepress.dev/reference/site-config
export default defineConfig({
  title: 'mail-archive',
  description: 'Email archive and analysis',
  lang: 'en-GB',
  cleanUrls: true,
  lastUpdated: true,

  // Without this the built site asks for /favicon.ico on every page and gets a
  // 404. The file lives in docs/public/, which VitePress copies verbatim.
  head: [['link', { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }]],

  // The diagram sources live beside the SVGs they generate. They are not pages,
  // and Vite must not try to parse the Python or the mxGraph XML.
  srcExclude: ['**/*.drawio'],

  // A dead link fails the build, which is what keeps the cross-references
  // honest. The one exception is the development server the getting-started
  // page tells you to open: it is unreachable at build time by definition, and
  // linking it is what makes it clickable while you follow along.
  ignoreDeadLinks: [/^https?:\/\/localhost(:\d+)?/],

  themeConfig: {
    // https://vitepress.dev/reference/default-theme-config
    nav: [
      { text: 'Using it', link: '/user/getting-started', activeMatch: '/user/' },
      {
        text: 'Building on it',
        link: '/developer/architecture',
        activeMatch: '/developer/',
      },
      { text: 'Diagrams', link: '/diagrams/', activeMatch: '/diagrams/' },
    ],

    sidebar: [
      {
        text: 'Using it',
        collapsed: false,
        items: [
          { text: 'Getting started', link: '/user/getting-started' },
          { text: 'Connecting a mailbox', link: '/user/connecting-a-mailbox' },
          { text: 'Importing mail', link: '/user/importing-mail' },
          { text: 'Semantic search', link: '/user/semantic-search' },
          { text: 'Configuration', link: '/user/configuration' },
          { text: 'The desktop app', link: '/user/desktop-app' },
          { text: 'Troubleshooting', link: '/user/troubleshooting' },
        ],
      },
      {
        text: 'Building on it',
        collapsed: false,
        items: [
          { text: 'Architecture', link: '/developer/architecture' },
          { text: 'Data model', link: '/developer/data-model' },
          { text: 'The import pipeline', link: '/developer/import-pipeline' },
          { text: 'Jobs and the worker', link: '/developer/jobs-and-worker' },
          { text: 'Adding a mail provider', link: '/developer/adding-a-provider' },
          { text: 'The MCP server', link: '/developer/mcp-server' },
          { text: 'Testing', link: '/developer/testing' },
          { text: 'Operations', link: '/developer/operations' },
        ],
      },
      {
        text: 'Reference',
        collapsed: false,
        items: [{ text: 'Diagram sources', link: '/diagrams/' }],
      },
    ],

    outline: { level: [2, 3] },

    search: { provider: 'local' },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/jenreh/mail-archive' },
    ],

    editLink: {
      pattern: 'https://github.com/jenreh/mail-archive/edit/main/docs/:path',
      text: 'Edit this page on GitHub',
    },

    footer: {
      message: 'Released under the MIT License.',
      copyright: 'Copyright © Jens Rehpöhler',
    },
  },
})
