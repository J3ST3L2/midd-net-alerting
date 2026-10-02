# Mist Vector ingress

This directory is the source of truth for the Vector service that receives
Juniper Mist webhooks on Gravitron and normalizes them into Keep alerts.

Production flow:

```text
Mist -> https://keep.middlebury.edu/mist-webhook
     -> Nginx
     -> Vector 127.0.0.1:8686
     -> Keep 127.0.0.1:8088/alerts/event
```

The Vector HTTP source intentionally listens only on loopback. Nginx owns the
public TLS endpoint.

## Event Hubs

The previous Vector configuration also consumed an Azure Event Hubs Kafka
source. That namespace no longer resolves and the direct HTTPS webhook path is
the production path, so Event Hubs is not included in this source-controlled
configuration.

## Secrets

No credentials belong in this directory. Mist signature headers are captured by
the HTTP source for future verification, but signature validation is not yet
implemented in this Vector configuration.

## Deployment

The production stack currently lives at `/opt/stacks/keep-mist-vector`.
Validate the repository configuration with the production Vector image before
replacing the live file, then recreate only the Vector service.
