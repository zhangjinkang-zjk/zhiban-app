const PPTIST_PAYLOAD_KEY = 'zhiban:pptist:payload'

const WIDTH = 1000
const HEIGHT = 562.5

const PALETTES = {
  default: ['#163f8f', '#5f8fc3', '#c9dce9', '#ffffff'],
  minimal: ['#111216', '#3b3f4a', '#6b6f7a', '#ffffff'],
  dark: ['#7aa2f7', '#bb9af7', '#7dcfff', '#1a1b26'],
  warm: ['#d94860', '#e36a2d', '#f2a341', '#fff7ef'],
  green: ['#11695f', '#28b487', '#a7f3d0', '#f6fffb']
}

const escapeHtml = value => String(value || '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;')

const cleanText = value => String(value || '')
  .replace(/<!--[\s\S]*?-->/g, ' ')
  .replace(/<\/?[^>\n]+>/g, ' ')
  .replace(/^#{1,6}\s+/gm, '')
  .replace(/^[\-*+•]\s+/gm, '')
  .replace(/\*\*(.*?)\*\*/g, '$1')
  .replace(/\n{3,}/g, '\n\n')
  .trim()

const splitBlocks = value => cleanText(value)
  .split(/\r?\n/)
  .map(line => line.trim())
  .filter(Boolean)
  .slice(0, 8)

const estimateLines = (value, charsPerLine = 33) => Math.max(1, Math.ceil(String(value || '').length / charsPerLine))

const paginateBlocks = (blocks, {
  maxLines = 12,
  maxItems = 5,
  charsPerLine = 33
} = {}) => {
  const pages = []
  let current = []
  let lineCount = 0

  for (const block of blocks) {
    const text = cleanText(block)
    if (!text) continue

    const blockLines = estimateLines(text, charsPerLine)
    const shouldStartNewPage = current.length && (
      lineCount + blockLines > maxLines ||
      current.length >= maxItems
    )

    if (shouldStartNewPage) {
      pages.push(current)
      current = []
      lineCount = 0
    }

    current.push(text)
    lineCount += blockLines
  }

  if (current.length) pages.push(current)
  return pages.length ? pages : [[]]
}

const htmlParagraph = (value, { size = 24, color = '#333333', bold = false, align = 'left' } = {}) => {
  const tagOpen = bold ? '<strong>' : ''
  const tagClose = bold ? '</strong>' : ''
  return `<p style="text-align:${align};">${tagOpen}<span style="font-size:${size}px;color:${color};">${escapeHtml(value)}</span>${tagClose}</p>`
}

const htmlList = (items, { size = 22, color = '#333333' } = {}) => {
  if (!items.length) return htmlParagraph('', { size, color })
  return `<ul style="font-size:${size}px;color:${color};">${items.map(item => `<li><p>${escapeHtml(item)}</p></li>`).join('')}</ul>`
}

const textElement = (id, content, left, top, width, height, options = {}) => ({
  type: 'text',
  id,
  left,
  top,
  width,
  height,
  rotate: 0,
  content,
  defaultFontName: options.font || '',
  defaultColor: options.color || '#333333',
  lineHeight: options.lineHeight || 1.25,
  fixedHeight: Boolean(options.fixedHeight),
  vAlign: options.vAlign || 'top',
  ...(options.fill ? { fill: options.fill } : {}),
  ...(options.textType ? { textType: options.textType } : {}),
  ...(options.inset ? { inset: options.inset } : {})
})

const rectElement = (id, left, top, width, height, fill, options = {}) => ({
  type: 'shape',
  id,
  left,
  top,
  width,
  height,
  viewBox: [200, 200],
  path: 'M 0 0 L 200 0 L 200 200 L 0 200 Z',
  fill,
  fixedRatio: false,
  rotate: 0,
  ...(options.opacity ? { opacity: options.opacity } : {}),
  ...(options.radius ? { radius: options.radius } : {})
})

const paletteForTheme = themeId => {
  const id = String(themeId || '').toLowerCase()
  if (id.includes('dark') || id.includes('night') || id.includes('mocha') || id.includes('terminal')) return PALETTES.dark
  if (id.includes('warm') || id.includes('sunset') || id.includes('retro')) return PALETTES.warm
  if (id.includes('green') || id.includes('science')) return PALETTES.green
  if (id.includes('minimal') || id.includes('white')) return PALETTES.minimal
  return PALETTES.default
}

const normalizeSlide = slide => {
  const title = cleanText(slide?.title || slide?.heading || 'Untitled')
  const bodySource = slide?.text || slide?.content || slide?.body || ''
  const blocks = Array.isArray(slide?.blocks) && slide.blocks.length
    ? slide.blocks.map(block => cleanText(block?.text || block?.content || block)).filter(Boolean)
    : splitBlocks(bodySource)
  const notes = cleanText(slide?.notes || slide?.speaker_notes || '')
  return { title, blocks, notes }
}

const buildCoverSlide = (slide, index, palette) => {
  const [primary, secondary, accent, paper] = palette
  const id = `zhiban_slide_${index + 1}`
  return {
    id,
    background: { type: 'solid', color: paper },
    remark: slide.notes || '',
    elements: [
      rectElement(`${id}_band`, 0, 0, 1000, 562.5, primary, { opacity: 0.08 }),
      rectElement(`${id}_left`, 0, 0, 18, 562.5, primary),
      rectElement(`${id}_accent`, 72, 365, 250, 8, accent),
      textElement(`${id}_title`, htmlParagraph(slide.title, { size: 46, color: primary, bold: true }), 72, 145, 820, 150, {
        color: primary,
        textType: 'title',
        fixedHeight: true,
        vAlign: 'middle'
      }),
      textElement(`${id}_sub`, htmlParagraph(slide.blocks.slice(0, 2).join('  '), { size: 20, color: secondary }), 76, 315, 760, 92, {
        color: secondary,
        textType: 'subtitle'
      })
    ]
  }
}

const buildContentSlide = (slide, index, palette) => {
  const [primary, secondary, accent, paper] = palette
  const id = `zhiban_slide_${index + 1}`
  const blocks = slide.blocks.length ? slide.blocks : ['No content']
  const estimatedLines = blocks.reduce((sum, block) => sum + estimateLines(block), 0)
  const dense = estimatedLines > 10 || blocks.length > 4
  const bodyFontSize = estimatedLines > 13 ? 17 : estimatedLines > 10 ? 18 : blocks.length > 5 ? 19 : 22
  const bodyWidth = dense ? 820 : 600
  return {
    id,
    background: { type: 'solid', color: paper },
    remark: slide.notes || '',
    elements: [
      rectElement(`${id}_top`, 0, 0, 1000, 72, primary, { opacity: 0.92 }),
      rectElement(`${id}_accent`, 64, 104, 68, 6, accent),
      textElement(`${id}_title`, htmlParagraph(slide.title, { size: 30, color: '#ffffff', bold: true }), 58, 14, 850, 52, {
        color: '#ffffff',
        textType: 'title',
        fixedHeight: true,
        vAlign: 'middle'
      }),
      textElement(`${id}_body`, htmlList(blocks, { size: bodyFontSize, color: '#1f2937' }), 74, 124, bodyWidth, 390, {
        color: '#1f2937',
        textType: 'content',
        lineHeight: dense ? 1.18 : 1.3,
        fixedHeight: true
      }),
      ...(!dense ? [
        rectElement(`${id}_visual`, 720, 135, 208, 248, secondary, { opacity: 0.14 }),
        rectElement(`${id}_visual_bar`, 720, 135, 208, 14, secondary),
        textElement(`${id}_visual_text`, htmlParagraph(slide.title, { size: 20, color: primary, bold: true, align: 'center' }), 742, 220, 164, 82, {
          color: primary,
          fixedHeight: true,
          vAlign: 'middle'
        })
      ] : [])
    ]
  }
}

const expandSlidesForPptist = slides => {
  const expanded = []

  slides.forEach((slide, slideIndex) => {
    if (slideIndex === 0) {
      expanded.push(slide)
      return
    }

    const pages = paginateBlocks(slide.blocks, {
      maxLines: 12,
      maxItems: 5,
      charsPerLine: 34
    })

    pages.forEach((blocks, pageIndex) => {
      expanded.push({
        ...slide,
        title: pageIndex ? `${slide.title} (cont.)` : slide.title,
        blocks
      })
    })
  })

  return expanded
}

export const toPptistPayload = ({ title = 'Zhiban PPT', slides = [], themeId = '' } = {}) => {
  const palette = paletteForTheme(themeId)
  const normalizedSlides = (Array.isArray(slides) ? slides : [])
    .map(normalizeSlide)
    .filter(slide => slide.title || slide.blocks.length)
  const pptistSlides = expandSlidesForPptist(normalizedSlides)

  return {
    title,
    viewportSize: WIDTH,
    viewportRatio: HEIGHT / WIDTH,
    theme: {
      themeColors: palette.slice(0, 3).concat(['#ffc000', '#4472c4', '#70ad47']),
      fontColor: '#333333',
      fontName: '',
      backgroundColor: palette[3],
      shadow: { h: 3, v: 3, blur: 2, color: '#808080' },
      outline: { width: 2, color: palette[0], style: 'solid' }
    },
    slides: pptistSlides.map((slide, index) => (
      index === 0
        ? buildCoverSlide(slide, index, palette)
        : buildContentSlide(slide, index, palette)
    ))
  }
}

export const openPptistEditor = payload => {
  const key = `${PPTIST_PAYLOAD_KEY}:${Date.now()}`
  localStorage.setItem(key, JSON.stringify(payload))
  window.open(`/pptist/index.html?source=${encodeURIComponent(key)}`, '_blank', 'noopener,noreferrer')
}
