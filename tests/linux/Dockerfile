# Linux test environment: run the end-to-end tests in the real srt sandbox
# (bubblewrap). Build from the repository root:
#   docker build -f tests/linux/Dockerfile -t claude-secure-sandbox-test .
#   docker run --rm --privileged claude-secure-sandbox-test
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 ripgrep bubblewrap socat jq fd-find openssh-client procps curl \
      ca-certificates util-linux \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m dev
USER dev
WORKDIR /home/dev/claude-secure-sandbox
COPY --chown=dev package.json package-lock.json ./
RUN npm ci --silent
COPY --chown=dev . .
CMD ["npm", "test"]
