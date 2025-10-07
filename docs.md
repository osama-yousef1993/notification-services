# FastAPI Notification Microservices — Full Implementation Plan

This document is a practical, step-by-step plan to build a **Notification microservices system** using **FastAPI**. It covers architecture, data model, messaging patterns (RabbitMQ + Redis hybrid), code outline (API + workers + WebSocket gateway), deployment (Docker Compose), testing, observability, security, and operational concerns.

---

## Goals & non-goals

**Goals**

* Build modular microservices for **email**, **SMS**, **push**, and **in-app** notifications.
* Low-latency real-time in-app delivery (WebSocket + Redis Pub/Sub).
* Reliable background processing and retry semantics (RabbitMQ with DLQ).
* Persistent notification history (Postgres) and fast unread counts (Redis).
* Idempotency and deduplication.

**Non-goals**

* Provide vendor-specific account setup (e.g., Twilio or FCM). We'll show example integration points.

---

## High-level architecture

```
Producer Services (app, auth, orders, etc.)
          |
          v
   Notification API (FastAPI)  <-- REST to create a notification
          |
          v
     RabbitMQ Exchange (topic)
     /       |         \
email_q   push_q   inapp_q  sms_q
  |         |        |       |
email worker push worker inapp worker sms worker
  \         |        /       /
   Save to Postgres (notification table)
          |
          v
   Publish to Redis Pub/Sub -> WebSocket Gateway
          |
          v
      Active user receives real-time update
```

**Flow**

1. External service calls Notification API to create a notification event.
2. Notification API

   * validates request, stores primary record (optional synchronous), and publishes an event to RabbitMQ exchange with routing key like `notifications.user.<user_id>` or `notifications.type.email`.
3. Dedicated workers consume from RabbitMQ queues, attempt delivery to channel (email/sms/push/in-app).
4. On success, worker updates `delivered_at` in DB; on failure, worker retries using RabbitMQ retry/DLQ pattern.
5. Worker publishes a small message to Redis Pub/Sub channel `user:{user_id}:notifications` so WebSocket gateway can push to connected clients.
6. WebSocket gateway subscribes to Redis channels and pushes to connected browsers/mobile clients.

---

## Components & responsibilities

1. **Notification API (FastAPI)**

   * Create notifications, query notifications, mark read, unread count.
   * Publish event to RabbitMQ.
   * Optionally write primary notification record to Postgres.

2. **Workers (Python Async)**

   * Consume from RabbitMQ.
   * Use provider clients (SMTP, Twilio, FCM) to deliver.
   * Update DB, publish Redis event, handle errors/retries.

3. **WebSocket Gateway (FastAPI)**

   * Maintain WebSocket connections.
   * Subscribe to Redis Pub/Sub for user channels and push to clients.

4. **Postgres**

   * Notifications table, indexing, read/unread state.

5. **Redis**

   * Fast unread counts, ephemeral storage, Pub/Sub for real-time gateway.

6. **RabbitMQ**

   * Topic exchange with per-channel queues, DLQ and retry queue.

---

## Data model (Postgres)

```sql
CREATE TABLE notifications (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  type VARCHAR NOT NULL, -- email,sms,push,inapp
  channel VARCHAR NOT NULL,
  payload JSONB NOT NULL,
  template_name VARCHAR NULL,
  status VARCHAR NOT NULL DEFAULT 'pending', -- pending, delivered, failed
  attempts INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
  delivered_at TIMESTAMP WITH TIME ZONE NULL,
  read_at TIMESTAMP WITH TIME ZONE NULL,
  dedup_key VARCHAR NULL
);

CREATE INDEX ON notifications (user_id, status);
CREATE INDEX ON notifications (dedup_key);
```

**Redis keys**

* `user:{user_id}:unread_count` (integer)
* `user:{user_id}:notifications` (Pub/Sub channel name)
* Optional: `dedup:{dedup_key}` with short TTL to prevent duplicate sends

---

## RabbitMQ topology

* Exchange: `notifications.exchange` (type: topic)
* Queues:

  * `notifications.email` (bind key `notifications.channel.email`)
  * `notifications.sms` (bind key `notifications.channel.sms`)
  * `notifications.push` (bind key `notifications.channel.push`)
  * `notifications.inapp` (bind key `notifications.channel.inapp`)
  * `notifications.dlx` (dead-letter queue for failed messages)

**Retry strategy**

* Use a per-queue dead-letter exchange and delayed requeue (or use RabbitMQ delayed-message-plugin) to implement exponential backoff.
* After `N` attempts, message routed to DLQ for manual inspection or automatic alerting.

---

## API design (FastAPI)

### Endpoints

* `POST /v1/notifications` — create notification event
* `GET /v1/notifications` — list notifications (with pagination)
* `GET /v1/notifications/unread_count` — returns unread count
* `POST /v1/notifications/{id}/read` — mark as read
* `POST /v1/notifications/bulk/read` — mark multiple as read

### Example request body for `POST /v1/notifications`

```json
{
  "user_id": "uuid",
  "type": "transaction",
  "channels": ["inapp","email"],
  "payload": {"amount": 12.5, "currency": "USD", "tx_id": ".."},
  "template": "transaction_alert",
  "dedup_key": "tx-12345-v1"
}
```

**Behavior**

* Validate and save minimal record.
* Publish a message to RabbitMQ for each channel required using routing keys like `notifications.channel.email`.
* Return 202 Accepted with notification id(s).

---

## Example FastAPI snippets

> The full code files are provided as examples here — use them as templates in your repo.

### Minimal API producer (conceptual)

```py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uuid, json

app = FastAPI()

class NotificationCreate(BaseModel):
    user_id: str
    type: str
    channels: list[str]
    payload: dict
    template: str | None = None
    dedup_key: str | None = None

@app.post('/v1/notifications')
async def create_notification(n: NotificationCreate):
    notification_id = str(uuid.uuid4())
    # 1. Persist to Postgres (synchronously or in a fast transaction)
    # 2. Publish to RabbitMQ per channel
    for ch in n.channels:
        msg = {
            'notification_id': notification_id,
            'user_id': n.user_id,
            'channel': ch,
            'payload': n.payload,
            'template': n.template,
            'dedup_key': n.dedup_key,
        }
        # publish_to_rabbitmq(exchange='notifications.exchange', routing_key=f'notifications.channel.{ch}', body=json.dumps(msg))

    return {'notification_id': notification_id}
```

### Worker consumer (conceptual async outline)

Use `aio_pika` (async RabbitMQ client) or `kombu` / `pika` if you prefer sync.

```py
import asyncio
import aio_pika

async def process_message(message: aio_pika.IncomingMessage):
    async with message.process(requeue=False):
        data = json.loads(message.body)
        # 1. Check dedup in Redis
        # 2. Attempt send via provider
        # 3. On success: update DB, publish Redis channel
        # 4. On failure: raise or nack, RabbitMQ will handle retry

async def main():
    connection = await aio_pika.connect_robust('amqp://guest:guest@rabbitmq/')
    channel = await connection.channel()
    queue = await channel.declare_queue('notifications.email', durable=True)
    await queue.consume(process_message)
    await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
```

### WebSocket gateway (FastAPI + aioredis)

```py
from fastapi import FastAPI, WebSocket
import aioredis, json

app = FastAPI()
redis = await aioredis.from_url('redis://redis')

@app.websocket('/ws/{user_id}')
async def ws_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f'user:{user_id}:notifications')

    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=10)
            if msg:
                await websocket.send_text(msg['data'].decode())
            # also handle inbound pings from client if needed
    finally:
        await pubsub.unsubscribe(f'user:{user_id}:notifications')
```

---

## Delivery provider integration points

* **Email**: SMTP (smtplib / aiosmtplib) or transactional provider (SendGrid, SES). Use templates (Jinja2) to render HTML/text.
* **SMS**: Twilio or vendor — use async HTTP client to call provider API.
* **Push**: FCM (Firebase) for Android/iOS (via HTTP v1 API) or APNs for iOS. Keep device tokens in DB with expiration and version.
* **In-app**: Delivery is publishing to Redis channel and storing event in Postgres for history.

Keep provider adapters small and testable (strategy pattern). Wrap provider calls with timeouts and circuit-breaker behavior.

---

## Reliability, Idempotency, and Retries

* **Deduplication**: Use `dedup_key` from producer; store `dedup:{key}` in Redis with TTL and check before processing.
* **Idempotency**: Ensure workers update DB using `ON CONFLICT` or idempotent updates so reprocessing the same notification doesn't duplicate.
* **Retries**: Use RabbitMQ TTL-based retry or delayed plugin. Maintain `attempts` counter in message or DB.
* **DLQ**: Move messages to `notifications.dlx` after configured retries.

---

## Observability

* **Metrics**: instrument API and workers with Prometheus metrics (requests, processing latency, delivery success/failures, queue depth).
* **Tracing**: OpenTelemetry tracing across API -> RabbitMQ -> workers -> provider calls.
* **Logging**: structured logs (JSON) with `notification_id`, `user_id`, `attempts`, and `provider_response`.
* **Alerts**: set alerts for DLQ growth, delivery error rate spikes, and large increases in queue backlog.

---

## Security

* Authenticate API endpoints (JWT/OAuth2) and validate producers.
* Encrypt PII in DB where necessary.
* Protect RabbitMQ with TLS and credentials; limit network access.
* Rate-limit notification creation to avoid spam.

---

## Testing strategy

* **Unit tests** for template rendering, provider adapters (use mocked HTTP/SMTP servers).
* **Integration tests** with a docker-compose test environment (RabbitMQ, Redis, Postgres) using pytest-asyncio.
* **Load tests** to verify queueing/backpressure behavior (k6 or Locust).

---

## Deployment & infra (Docker Compose example)

Services:

* `api` (FastAPI)
* `worker-email`, `worker-sms`, `worker-push`, `worker-inapp`
* `ws-gateway` (FastAPI WebSocket)
* `postgres`
* `redis`
* `rabbitmq`
* `prometheus`, `grafana` (optional)

A simple `docker-compose.yml` will wire them for dev. In production, run each service in its own autoscaled container (Kubernetes or ECS), with RabbitMQ & Redis managed or clustered.

---

## Recommended folder structure

```
notifications/
├─ api/
│  ├─ app/main.py
│  ├─ app/routes/notifications.py
│  ├─ app/schemas.py
│  └─ Dockerfile
├─ workers/
│  ├─ email_worker/
│  ├─ sms_worker/
│  └─ push_worker/
├─ ws_gateway/
│  └─ main.py
├─ shared/
│  ├─ lib/rabbit.py
│  ├─ lib/redis_client.py
│  └─ lib/db.py
├─ infra/docker-compose.yml
└─ tests/
```

---

## CI/CD & rollout

* Build images per service, run unit and integration tests in CI.
* Use blue/green or rolling deployment for API and workers.
* Migrate DB with versioned Alembic scripts.

---

## Operational playbook (short)

* If DLQ grows: inspect messages, fix provider or data, replay messages.
* If unread counts mismatch: re-sync from DB to Redis for affected users.
* If high failure rate to a provider: circuit-break and route to fallback or alert ops.

---

## Next implementation phases (iterative)

1. MVP: API + in-app worker + WebSocket gateway + Postgres + Redis. Deliver real-time in-app notifications.
2. Add Email worker and SMTP integration. Add templates.
3. Add SMS and Push workers and provider adapters.
4. Harden with retries, DLQ, monitoring, and security.

---

## Appendix: Useful patterns & tips

* Use `UUID` for `notification_id` to avoid collision.
* Trim payloads: store large content in cloud object store and keep pointers in DB.
* For mobile push, maintain device token versions per user and use batch sends when possible.
* For high-throughput, partition queues by user shard (e.g., `user_bucket_{n}`) to avoid hot-queues.

---

If you want, I can now:

* generate a working **starter repo** scaffold (FastAPI + example worker + docker-compose) as files,
* OR produce a detailed **Docker Compose** and **Kubernetes** deployment manifest,
* OR write the **email worker** complete code with SMTP (aiosmtplib) and Jinja2 templates.

Tell me which of those you'd like and I will generate the files next.
