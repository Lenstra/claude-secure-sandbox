# Linux test environment: run the end-to-end tests in the real srt sandbox
# (bubblewrap). Build from the repository root:
#   docker build -f tests/linux/Dockerfile -t claude-secure-sandbox-test .
#   docker run --rm --privileged claude-secure-sandbox-test
# On an Ubuntu 24.04 host, first allow user namespaces, which containers share:
#   sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 ripgrep bubblewrap socat jq fd-find openssh-client procps curl \
      ca-certificates util-linux cron at \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m dev
USER dev
# Tests outside /tmp: once protected code exists, the sandbox hides /tmp, and a
# real home directory never lives there.
RUN mkdir -p /home/dev/tmp
ENV TMPDIR=/home/dev/tmp
WORKDIR /home/dev/claude-secure-sandbox
COPY --chown=dev package.json package-lock.json ./
RUN npm ci --silent
COPY --chown=dev . .
CMD ["npm", "test"]
