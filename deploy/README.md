# Gravitron deployment wiring

GitHub is the source of truth for the Keep workflow configuration in this repository.

The existing Keep runtime remains in:

```text
/opt/stacks/keep
```

The Git checkout remains in:

```text
/opt/stacks/keep-config
```

## Compose override

`gravitron-compose.override.yaml` adds the GitHub-managed workflow directory to the existing Keep backend:

- host: `/opt/stacks/keep-config/keep/workflows`
- container: `/config/workflows`
- environment: `KEEP_WORKFLOWS_DIRECTORY=/config/workflows`

The workflow mount is read-only. Runtime state and provider secrets remain outside Git.

Install the override by symlinking it into the Keep runtime directory:

```bash
sudo ln -sfn \
  /opt/stacks/keep-config/deploy/gravitron-compose.override.yaml \
  /opt/stacks/keep/compose.override.yaml
```

Docker Compose automatically combines `compose.yaml` and `compose.override.yaml`.

## Deploy after a GitHub change

```bash
cd /opt/stacks/keep-config
git pull --ff-only

cd /opt/stacks/keep
sudo docker compose config >/dev/null
sudo docker compose up -d --force-recreate keep-backend
```

Then verify:

```bash
BACKEND="$(sudo docker ps --format '{{.Names}}' | grep -E 'keep.*backend' | head -1)"

sudo docker inspect "$BACKEND" \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^KEEP_WORKFLOWS_DIRECTORY='

sudo docker inspect "$BACKEND" \
  --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}' \
  | grep '/config/workflows'

sudo docker exec "$BACKEND" sh -lc \
  'ls -l "$KEEP_WORKFLOWS_DIRECTORY"'
```

Expected wiring:

```text
KEEP_WORKFLOWS_DIRECTORY=/config/workflows
/opt/stacks/keep-config/keep/workflows -> /config/workflows
```

Keep imports provisioned workflows from this directory on backend startup.
