import type { ThemeId } from "../types";

const THEMES: { id: ThemeId; label: string }[] = [
  { id: "phosphor", label: "PHOSPHOR" },
  { id: "cyberpunk", label: "CYBERPUNK" },
  { id: "matrix", label: "MATRIX" },
  { id: "tron", label: "TRON GRID" },
  { id: "amber", label: "AMBER RETRO" },
];

interface Props {
  theme: ThemeId;
  onChange: (theme: ThemeId) => void;
}

export function ThemeSwitcher({ theme, onChange }: Props) {
  return (
    <select
      className="theme-switcher"
      value={theme}
      onChange={(e) => onChange(e.target.value as ThemeId)}
      aria-label="Console theme"
    >
      {THEMES.map((t) => (
        <option key={t.id} value={t.id}>
          {t.label}
        </option>
      ))}
    </select>
  );
}
