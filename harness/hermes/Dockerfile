# Harness container for Hermes Agent
# Uses git clone for reproducible, versioned deployments
#
# Build: docker build -f harness/hermes/Dockerfile -t hermes-harness:latest .
# Run:   docker run -v ~/.hermes:/opt/hermes-data hermes-harness:latest
#
FROM debian:13.4

# Disable Python stdout buffering to ensure logs are printed immediately
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential nodejs npm python3 python3-venv python3-pip \
        curl git procps && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user for runtime
RUN useradd -m -d /opt/hermes hermes

# Set up directories
RUN mkdir -p /opt/hermes-source /opt/hermes-data && chown hermes:hermes /opt/hermes-source /opt/hermes-data

WORKDIR /opt/hermes-source

# Clone Hermes repository
# Build arg allows specifying branch/tag/commit (defaults to main)
ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent-hosted.git
ARG HERMES_REF=main

RUN git init && \
    git remote add origin "$HERMES_REPO" && \
    git fetch --depth 1 origin "$HERMES_REF" && \
    git checkout FETCH_HEAD

# Run setup as hermes user
USER hermes
RUN ./setup-hermes.sh

# Set up environment
ENV HERMES_HOME=/opt/hermes-data

# Slack Webhook Platform Environment Variables
# These can be overridden at runtime via docker run -e
ENV ORG_ID=
ENV TENANT_ID=
ENV USER_ID=
ENV CHANNEL_ID=
ENV SLACK_BOT_TOKEN=
ENV SLACK_SIGNING_SECRET=
ENV SLACK_ALLOWED_USERS=
ENV STATE_DIR=/var/lib/hermes/state

# Default to Slack webhook platform only
ENV SLACK_WEBHOOK_ENABLED=true
ENV HERMES_GATEWAY_BUSY_INPUT_MODE=hidden

VOLUME [ "/opt/hermes-data" ]

# Entry point runs hermes
ENTRYPOINT [ "/opt/hermes-source/hermes" ]
CMD []
