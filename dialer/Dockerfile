# Reachwell Orchestrator — single-container deploy (SPEC: no Kubernetes).
# The same image runs both roles:
#   server:  docker run <img>                    (default CMD: orchestrator serve)
#   worker:  docker run <img> orchestrator worker
FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY orchestrator ./orchestrator
COPY migrations ./migrations
COPY config ./config
COPY prompts ./prompts
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
# tzdata: calling-window math needs real zoneinfo for US timezones.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 orchestrator
COPY --from=build /install /usr/local
# Non-code assets ship beside the package (paths resolve via PROJECT_ROOT).
COPY --from=build /app/migrations /usr/local/lib/python3.12/site-packages/migrations
COPY --from=build /app/config /usr/local/lib/python3.12/site-packages/config
COPY --from=build /app/prompts /usr/local/lib/python3.12/site-packages/prompts
USER orchestrator
EXPOSE 8080
CMD ["orchestrator", "serve"]
