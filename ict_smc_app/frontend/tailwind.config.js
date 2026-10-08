/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#060b16", 900: "#0b1220", 850: "#0f1729", 800: "#131d33", 700: "#1d2a45", 600: "#2a3a5c" },
        gold: { 400: "#e8c35a", 500: "#d4a72c", 600: "#a8821c" },
        bull: { 400: "#34d399", 500: "#10b981", 900: "#052e22" },
        bear: { 400: "#f87171", 500: "#ef4444", 900: "#3b0d0d" },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "Segoe UI", "Roboto", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
