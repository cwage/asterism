# Playwright's own image: Chromium and its system libraries at the version
# the pinned @playwright/test expects. Keep the two versions the same.
FROM mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27

WORKDIR /browser
COPY package.json package-lock.json ./
RUN npm ci
# Baked in, like the app's own tests: rebuild after editing (compose's
# --build does it).
COPY . .
CMD ["npx", "playwright", "test"]
