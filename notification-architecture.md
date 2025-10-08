# Notification Service Architecture Guide

## Recommendation Summary

### Option 1: Minimal Cross-Cloud Complexity (Recommended)
If you want minimal cross-cloud complexity and lowest latency: keep everything on DigitalOcean and use Redis Streams + Redis Pub/Sub (Managed Redis) + background workers (Dramatiq/Celery or custom asyncio). This integrates cleanly with your FastAPI + Postgres.

### Option 2: Managed Scaling
If you want managed scaling and serverless and don't mind cross-cloud ops: keep FastAPI on DO but publish events to GCP Pub/Sub and use Cloud Functions / Cloud Run for workers. This gives auto-scaling and low ops cost, but adds cross-cloud auth, egress, and slightly higher latency.

Below I first present a DO-native design (recommended) and then the GCP variant and their differences so you can choose.

## 1. High-Level Architecture (Recommended — DigitalOcean + Redis Streams)

```
Producers (your app services)
        ↓ (HTTP)
 FastAPI API (Cloud App on DO)  <-- writes to Postgres (primary store)
        ↓ (XADD) 
 Redis Streams: notifications.stream  (durable pub-sub queue)
        ↓ (XREADGROUP)
 Worker group(s) (email-worker, sms-worker, push-worker, inapp-worker)
        ↓ writes
 Postgres (notifications table)
        ↓ publishes
 Redis Pub/Sub channels: user:{user_id}:notifications
        ↓
 WebSocket Gateway (FastAPI on DO) --> Web UI (browser)
        ↓
 Push Worker -> FCM/APNs -> Mobile apps
```

### Architecture Components

- **Postgres**: canonical store of all notifications, user preferences, templates
- **Redis Streams**: queue with consumer groups per worker type. Reliable, fast, supports ack + pending list
- **Redis Pub/Sub**: realtime push to WebSocket gateway (low-latency fan-out)
- **Workers**: run as separate services (Docker on DO app platform / droplet / k8s). Each worker picks messages relevant to its channels
- **Push**: mobile via FCM/APNs (workers call provider APIs)
- **Dynamic channels**: controlled via channels table & generic webhook channel type

## 2. Data Model (Postgres) — Core Tables

### Notifications Table
```sql
-- notifications
CREATE TABLE notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type TEXT NOT NULL,           -- e.g., payment, invite, watchlist_change
  user_id UUID NOT NULL,
  actor_user_id UUID NULL,
  channels TEXT[] NOT NULL,           -- ["inapp","email"]
  payload JSONB NOT NULL,             -- domain payload
  template_name TEXT NULL,
  status TEXT NOT NULL DEFAULT 'pending', -- pending, delivered, failed, canceled
  attempts INT NOT NULL DEFAULT 0,
  dedup_key TEXT NULL,
  priority INT DEFAULT 100,
  created_at TIMESTAMPTZ DEFAULT now(),
  delivered_at TIMESTAMPTZ NULL,
  read_at TIMESTAMPTZ NULL
);
CREATE INDEX ON notifications (user_id, status);
CREATE INDEX ON notifications (dedup_key);
```

### Channels Table (Dynamic)
```sql
-- channels (dynamic)
CREATE TABLE channels (
  id SERIAL PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,          -- "email", "sms", "push", "inapp", "webhook"
  name TEXT NOT NULL,
  enabled BOOLEAN DEFAULT TRUE,
  config JSONB DEFAULT '{}'           -- provider config, e.g. {"provider":"sendgrid","key":"..."}, or webhook url
);
```

### User Channel Preferences
```sql
-- user_channel_prefs
CREATE TABLE user_channel_prefs (
  id SERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  channel_slug TEXT NOT NULL,
  opted_in BOOLEAN DEFAULT TRUE,
  constraints JSONB DEFAULT '{}'    -- for user per-channel filtering
);
CREATE UNIQUE INDEX ON user_channel_prefs (user_id, channel_slug);
```

### Device Tokens for Push
```sql
-- device tokens for push
CREATE TABLE device_tokens (
  id SERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  provider TEXT NOT NULL,   -- fcm/apns
  token TEXT NOT NULL,
  platform TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

### Templates (Jinja2)
```sql
-- templates (Jinja2)
CREATE TABLE templates (
  name TEXT PRIMARY KEY,
  channel_slug TEXT NOT NULL,
  subject TEXT,
  body TEXT,           -- template body (HTML/markdown/text)
  updated_at TIMESTAMPTZ DEFAULT now()
);
```

### Asset Subscriptions
```sql
-- watchlist subscriptions / asset subscriptions
CREATE TABLE asset_subscriptions (
  id SERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  asset_id TEXT NOT NULL,
  threshold NUMERIC NULL,
  last_notified_price NUMERIC NULL
);
```

### Notes

- `channels` lets you enable/disable channels dynamically and store provider config (for generic channels like webhook)
- `user_channel_prefs` allows per-user opt-in/out
- Keep secrets (API keys) out of DB; store in secret manager / environment variables

## 3. Notification Message Envelope (JSON)

This is what your API will create and your queue will carry:

```json
{
  "notification_id": "uuid",
  "event_type": "payment",
  "user_id": "uuid-owner",
  "actor_user_id": "uuid-buyer",
  "channels": ["inapp","email","sms"],
  "payload": {
    "amount": 12.5,
    "currency": "USD",
    "tx_id": "abc123",
    "product": "Premium Plan"
  },
  "template_name": "payment_receipt",
  "dedup_key": "tx-abc123-v1",
  "priority": 100,
  "created_at": "2025-10-08T12:00:00Z"
}
```

Workers must treat the message as the source of truth and perform validation & filtering based on user preferences and channel config.

## 4. API Design (FastAPI) — Endpoints & Behavior

### Essential Endpoints

#### POST /v1/notifications
- **Request body**: the envelope above
- **Behavior**:
  1. Validate & auth
  2. Write the notifications row (transactionally) — or write minimal record and mark pending
  3. Publish to queue (Redis Streams OR Pub/Sub) once
  4. Return 202 Accepted + notification_id

#### Other Endpoints
- `GET /v1/notifications?user_id=&limit=&offset=` — paginated list (for in-app history)
- `GET /v1/notifications/unread_count` — return counts (cache in Redis for speed)
- `POST /v1/notifications/{id}/read` — mark read
- `POST /v1/channels` (admin) — add/update channel config (dynamic channels)
- `GET /v1/channel-configs` — used by workers or admin UI

### Example FastAPI + Redis Streams Publish Snippet

```python
# simplified
import json, uuid, time
from aioredis import Redis, from_url

redis = await from_url("redis://:password@redis-host:6379", decode_responses=False)
STREAM = "notifications.stream"

async def publish_notification(payload: dict):
    payload_bytes = json.dumps(payload).encode()
    # XADD stream
    await redis.xadd(STREAM, {"data": payload_bytes}, maxlen=100000, approximate=True)
```

If you choose GCP Pub/Sub, replace publish with `publisher.publish(topic_path, json.dumps(payload).encode(), channel="email")` and configure service account creds.

## 5. Worker Design (Per-Channel Consumer)

Each worker process:

1. **Consume** from queue (Redis Streams consumer group or Pub/Sub subscriber)
2. **Deserialize** message
3. **Filter**:
   - Is channel enabled (channels table)?
   - Has user opted out (user_channel_prefs)?
   - Dedup check: dedup_key presence (Redis SETNX or DB unique index)
4. **Deliver**:
   - **Email**: use SendGrid / SES or SMTP (aiosmtplib). Render Jinja2 templates
   - **SMS**: Twilio API or vendor
   - **Push**: FCM / APNs (store device tokens in device_tokens)
   - **In-app**: store in notifications (already created) and publish to `user:{user_id}:notifications` pubsub channel
   - **Watchlist / asset price**: logic worker that monitors price events and emits notifications when thresholds reached
5. **Acknowledge/Update**:
   - **on success**: mark delivered in DB, set delivered_at, increment attempts
   - **on failure**: increment attempts; if attempts < N then requeue with backoff (either re-enqueue in stream with metadata or use a retry stream); else write to DLQ table or stream and alert ops
6. **Publish realtime**: After successful delivery or creation, publish small message to Redis Pub/Sub `user:{user_id}:notifications` so WebSocket gateway pushes to browser

**Important**: Workers must be idempotent — use notification_id + dedup_key to ensure reprocessing doesn't duplicate sends.

## 6. Real-Time UX (Web + Mobile)

### Web (Browser)

**Option A (recommended on DO)**: run a WebSocket Gateway (FastAPI endpoint `/ws/{user_id}`) that subscribes to Redis Pub/Sub `user:{user_id}:notifications`. When worker publishes to that channel, gateway pushes to connected clients.

**Pros**: low-latency, simple.

**Concurrency**: run gateway as multiple instances; use Redis Pub/Sub so all instances get messages.

### Mobile (Push + In-App)

- **Push notifications**: use FCM for Android + Web push; APNs for iOS
- **Store device tokens** in device_tokens
- **Worker calls FCM/APNs** when channel == "push"
- **For in-app mobile UI**, you can also open a WebSocket (if app supports) or use background fetch to read `/v1/notifications`
- **Fallback**: If device not reachable, mark as unread and rely on in-app feed

## 7. Dynamic Channels (Add/Remove at Runtime)

### Goal
Be able to enable/disable channels and add simple new channels without full redeploy.

### Mechanism
- `channels` table controls enabled & config
- Worker reads channel configurations from DB (cache in Redis) and auto-refresh every N seconds or subscribe to a config-change channel
- Support a generic "webhook" channel type:
  - Admin creates a channel with `slug="webhook-telegram"` and `config={"url":"https://hooks...","method":"POST","auth":"Bearer .."}`
  - Worker for webhook will use the config to deliver. That allows adding new third-party integrations without code changes (for simple HTTP-based providers)

### When Code Changes Are Needed
- New channel types that need special protocol (e.g., APNs with advanced features) may require a coded worker; but the webhook approach covers a lot of use cases (discord, slack, custom services)
- For truly new logic (e.g., server-side complex aggregation), add a new worker microservice and register it by creating a channel row and worker subscribing to the stream where message has `channel: newchannel`

### Admin UI
Build an admin page to create/update channels, toggle enabled, test credentials (test send), and see DLQ messages.

## 8. Deduplication, Idempotency & Retries

### Dedup
Producers supply dedup_key (e.g., tx-12345-v1). Worker checks Redis set `dedup:{dedup_key}` with SETNX and TTL. Also enforce DB unique constraint on (dedup_key) optionally.

### Idempotency
Use notification_id as the idempotency key in providers where you can (e.g., store provider message_id in DB and ignore duplicates).

### Retries / DLQ
- **Redis Streams**: Use consumer group Pending Entries List (PEL). If message not acked, worker can claim and retry. Implement a retry counter & on exceed, move to notifications.dlq stream and insert into notifications_failed table
- **Pub/Sub**: Use built-in ack deadlines and Dead Letter Topics
- **Backoff**: exponential backoff before retry

## 9. Monitoring and Observability

### Track These Metrics (Prometheus + Grafana, or DO Monitoring)
- queue length (XINFO STREAM or stream length)
- consumer lag (pending count)
- delivery success/failure per channel
- per-provider errors and rate limit errors
- unread_count correctness
- DLQ size

### Logging
Structured logs (JSON) with notification_id, user_id, channel, attempt, status.

### Tracing
Add OpenTelemetry tracing for request→publish→worker→provider.

### Alerts
DLQ growth, high failure rate for a provider, long queue lag.

## 10. Security & Compliance

- **Auth**: secure API (JWT/OAuth) for producers. Only trusted services can call POST /v1/notifications
- **Secrets**: store API keys in environment variables or a secrets manager (DO has secret environment variables). Never store provider keys in DB as plaintext
- **Rate-limiting**: per-user and per-producer to avoid spam. Enforce limits on creates
- **GDPR / PII**: redact personal data in logs, enable data retention policies (delete old notifications)
- **SMS / Email legal**: ensure compliance for marketing messages (unsubscribe links in email, SMS opt-out)

## 11. Implementation Roadmap — Step by Step

### Phase 0 – Prep
- Provision a Managed Redis instance on DO (or self-host) and ensure network access from FastAPI and workers
- Create database migration scripts for the tables above (Alembic)

### Phase 1 – MVP (2–4 days)
- Implement DB schema + migration
- Implement POST /v1/notifications that writes row to notifications table and publishes to Redis Stream notifications.stream
- Implement a simple in-app worker:
  - Consumer group inapp-group reads stream, marks notification delivered, publishes to Redis Pub/Sub `user:{user_id}:notifications`
- Implement a WebSocket Gateway in FastAPI that subscribes to Redis Pub/Sub and pushes to connected browsers. Add basic auth
- Implement UI: in-app notifications list & real-time toast

### Phase 2 – Channels & Workers (3–7 days)
- Add channels and user_channel_prefs
- Implement email worker (SendGrid or aiosmtplib) + Jinja2 templates
- Implement sms worker (Twilio)
- Implement push worker (FCM): store device tokens and send pushes
- Add DLQ handling & simple admin UI for failed messages

### Phase 3 – Advanced Features (2+ weeks)
- Watchlist & asset price worker (subscribe to price feed; emit events)
- Implement deduplication & idempotency thoroughly
- Implement retry/backoff with DLQ
- Add metrics, tracing, and alerts
- Add generic webhook channel

### Phase 4 – Harden & Scale
- Scale workers horizontally; shard streams by user bucket if hot users appear
- Implement batching for email/push where possible
- Add rate-limiting & quota system
- Run load tests (k6 / locust)

## 12. If You Choose GCP Pub/Sub + Cloud Functions (Cross-Cloud Approach)

You can keep FastAPI on DigitalOcean and publish to GCP Pub/Sub. High level:

- FastAPI publishes to Pub/Sub topic `projects/{project}/topics/notifications`
- Create 1 subscription per channel (or use single subscription and filter by pubsub attributes)
- Cloud Functions subscribe and act as workers (email, sms, push, inapp)
- For real-time in-app: workers write to your Postgres or to Firebase / Firestore and you can rely on Firestore realtime listeners OR the worker publishes to your Redis Pub/Sub/Socket (but then cross-cloud egress)

### Authentication
FastAPI needs a service account JSON (store on DO securely) to authenticate with Google libraries.

### Pros
- Managed scaling
- DLQ
- Easier multi-region distribution

### Cons
- Cross-cloud network latency + egress cost
- More complex auth & secrets
- Cold-starts for small traffic

### If You Pick This
- Use google-cloud-pubsub client in FastAPI
- Use Pub/Sub message attributes for channel
- Use Dead Letter Topics
- Design similar DB schema in Postgres on DO. Workers should update your DO Postgres (need network access from GCP Functions to DO Postgres—setup public IP + secure connection or use Cloud SQL proxy with network peering; cross-cloud access is a bit awkward: prefer Cloud Run in GCP in same VPC or place DB to GCP)

**Important note**: If your Postgres stays on DO, Cloud Functions must have network access (public IP + SSL + firewall) to your DB. That's workable but adds network & security configuration. For low-ops cross-cloud simplicity, consider moving DB to Cloud SQL if you go heavy on GCP workers.

## 13. Libraries & Tooling (Python / FastAPI)

- **Async DB**: SQLAlchemy 2.0 + asyncpg or async SQLModel; Alembic for migrations
- **Redis**: aioredis / redis-py (v4) async client that supports streams
- **Queue/Worker frameworks**:
  - Dramatiq (with Redis broker) — simple, performant
  - RQ — simpler Redis queue for Python (sync)
  - Custom worker using aioredis.xreadgroup for precise control
- **Email**: sendgrid (HTTP) or aiosmtplib
- **SMS**: twilio python client (async via httpx)
- **Push**: pyfcm or call FCM HTTP v1 directly via aiohttp
- **WebSockets**: fastapi and starlette.websockets
- **Metrics + tracing**: prometheus_client, opentelemetry

## 14. Example Minimal Code Snippets

### Create Notification Endpoint (FastAPI + Postgres + Redis Streams)

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import json, uuid
from aioredis import from_url
from sqlalchemy.ext.asyncio import AsyncSession

app = FastAPI()
redis = await from_url("redis://redis:6379")
STREAM = "notifications.stream"

class CreateNotification(BaseModel):
    user_id: str
    event_type: str
    channels: list[str]
    payload: dict
    dedup_key: str | None = None

@app.post("/v1/notifications", status_code=202)
async def create_notification(req: CreateNotification, db: AsyncSession):
    notification_id = str(uuid.uuid4())
    # 1. insert row into notifications table (async)
    # 2. publish to redis stream
    msg = {
        "notification_id": notification_id,
        "event_type": req.event_type,
        "user_id": req.user_id,
        "channels": req.channels,
        "payload": req.payload,
        "dedup_key": req.dedup_key
    }
    await redis.xadd(STREAM, {"data": json.dumps(msg)})
    return {"notification_id": notification_id}
```

### Worker (In-App Consumer) — Conceptual

```python
async def inapp_worker():
    group = "inapp_group"
    consumer = "worker-1"
    await redis.xgroup_create(STREAM, group, id="$", mkstream=True)
    while True:
        resp = await redis.xreadgroup(group, consumer, {STREAM: ">"}, count=10, block=20000)
        for stream_name, messages in resp:
            for msg_id, fields in messages:
                data = json.loads(fields[b"data"])
                # process inapp: mark delivered in DB
                # publish to pubsub channel for websocket:
                await redis.publish(f"user:{data['user_id']}:notifications", json.dumps({
                    "id": data["notification_id"],
                    "payload": data["payload"]
                }))
                await redis.xack(STREAM, group, msg_id)
```

## 15. Pitfalls & Gotchas

- **Hot users**: a single high-activity user can overwhelm a worker or a single Redis stream consumer; consider sharding by user hash or use per-user queues for very hot flows
- **Email deliverability**: set SPF/DKIM/DMARC, monitor bounces
- **SMS cost & regulatory**: SMS is expensive—add rate-limits and opt-in checks
- **Cross-cloud DB access** (if using GCP workers + DO Postgres): secure network + performance considerations
- **Idempotency**: always assume at-least-once delivery; design idempotent sends
- **Provider rate limits**: implement throttling & batch sending for email/push

## 16. Suggested Monitoring & SLOs

### SLO Examples
- 99% of in-app notifications delivered < 1s
- 95% of push/email deliveries attempted within 30s
- DLQ messages < 0.1% of total processed

### Monitor
- notifications.created_per_minute
- notifications.processed
- notifications.failed
- queue_length
- pending_count

## 17. Quick Decision Checklist for You

- **Want lowest ops + fastest on existing infra** → DO + Redis Streams
- **Want serverless managed scaling and can accept cross-cloud complexity** → GCP Pub/Sub + Cloud Functions (but consider moving DB to GCP)
- **Need truly dynamic, no-code channels** → implement webhook channel + admin UI
- **Want mobile push** → use FCM (store tokens) + inapp fallback