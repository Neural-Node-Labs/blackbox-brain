/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_OLLAMA_MODEL?: string;
  readonly VITE_GATEWAY_ORIGIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
