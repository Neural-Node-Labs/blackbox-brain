import { FormEvent, useState } from "react";

interface Props {
  disabled: boolean;
  onSubmit: (text: string) => void;
}

export function InputBar({ disabled, onSubmit }: Props) {
  const [value, setValue] = useState("");

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    setValue("");
  }

  return (
    <form className="input-bar" onSubmit={handleSubmit}>
      <span className="input-bar__prompt">root@blackbox:~$</span>
      <input
        className="input-bar__field"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={disabled ? "transmission in progress…" : "enter directive…"}
        disabled={disabled}
        autoFocus
      />
      <button className="input-bar__send" type="submit" disabled={disabled || !value.trim()}>
        TRANSMIT
      </button>
    </form>
  );
}
