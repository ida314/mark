# Throwaway execution environment for shell_exec. No secrets, no docker socket, no host mounts
# other than the agent's own workspace.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ripgrep curl ca-certificates jq \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 sandbox
WORKDIR /workspace
USER sandbox
CMD ["bash"]
