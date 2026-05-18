export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      colors: {
        zbg: '#FDFBF6',
        zcard: '#FFFFFF',
        zgreen: '#2F6B2F',
        zgreenDark: '#1F4F24',
        zgreenSoft: '#EEF5EA',
        ztext: '#1F1F1A',
        zmuted: '#7A756B',
        zborder: '#E5DED3',
      },
      boxShadow: {
        zsoft: '0 12px 40px rgba(47, 107, 47, 0.08)',
      },
      borderRadius: {
        zcard: '28px',
      },
    },
  },
}