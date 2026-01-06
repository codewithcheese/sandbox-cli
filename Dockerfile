FROM node:20-bullseye

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git curl wget vim build-essential \
    openssh-client iproute2 \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh && rm -rf /var/lib/apt/lists/*

# Install global npm packages
RUN npm install -g @anthropic-ai/claude-code@latest pnpm

# Install Playwright system dependencies
RUN npx playwright install-deps

# Create agent user and directories
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /pnpm-store \
    && chown agent:agent /pnpm-store

# Switch to agent user
USER agent
WORKDIR /home/agent

# Install Playwright browsers
RUN npx playwright install

# Configure pnpm store location
ENV PNPM_STORE_DIR=/pnpm-store

# Python: ensure pip uses user site-packages
ENV PIP_USER=1
ENV PATH="/home/agent/.local/bin:${PATH}"

CMD ["claude"]
