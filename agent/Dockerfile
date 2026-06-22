# syntax=docker/dockerfile:1

# ---------- Stage 1: build ----------
FROM node:20-alpine AS builder

WORKDIR /build

COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY tsconfig.json vite.config.ts index.html ./
COPY src/ ./src/

# Optional build-time model override; baked into the static bundle.
ARG VITE_OLLAMA_MODEL=llama3
ENV VITE_OLLAMA_MODEL=${VITE_OLLAMA_MODEL}

RUN npx tsc -b && npx vite build

# ---------- Stage 2: runtime ----------
FROM nginx:1.27-alpine AS runtime

RUN addgroup -g 1000 console && adduser -D -u 1000 -G console console \
    && rm -rf /usr/share/nginx/html/* \
    && rm /etc/nginx/conf.d/default.conf

COPY --chown=console:console nginx.conf /etc/nginx/conf.d/console.conf
COPY --from=builder --chown=console:console /build/dist /usr/share/nginx/html

# nginx needs to write its pid file; /var/cache/nginx is also chowned
# here as a fallback for anyone running this image without the tmpfs
# mounts docker-compose.yml provides (those mounts override this at
# runtime, but the image should still work standalone).
RUN touch /var/run/nginx.pid \
    && chown console:console /var/run/nginx.pid \
    && chown -R console:console /var/cache/nginx

USER console

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8081/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
