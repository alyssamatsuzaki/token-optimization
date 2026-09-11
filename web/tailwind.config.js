/** Palette and type scale are defined in docs/DESIGN.md. Nothing outside that list ships. */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "#F2F3EF",
        chalk: "#FFFFFF",
        ink: "#15181A",
        graphite: "#5E6560",
        prussian: "#12456B",
        vermilion: "#C0452A",
        verdigris: "#1F6F5C",
        rule: "#15181A1F",
        "rule-strong": "#15181A33",
      },
      fontFamily: {
        sans: ["'IBM Plex Sans Variable'", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'IBM Plex Mono'", "ui-monospace", "monospace"],
      },
      fontSize: {
        micro: ["0.75rem", { lineHeight: "1.1rem" }],
        small: ["0.8125rem", { lineHeight: "1.2rem" }],
        base: ["0.875rem", { lineHeight: "1.35rem" }],
        body: ["1rem", { lineHeight: "1.5rem" }],
        head: ["1.25rem", { lineHeight: "1.6rem" }],
        title: ["1.75rem", { lineHeight: "2.1rem" }],
        figure: ["2.5rem", { lineHeight: "2.7rem" }],
      },
      transitionTimingFunction: { morph: "cubic-bezier(.2,.7,.3,1)" },
    },
  },
  plugins: [],
};
