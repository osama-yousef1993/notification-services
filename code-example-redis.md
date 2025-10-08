# Notification Service Architecture

A comprehensive guide for building a scalable, reliable notification service using FastAPI, Redis Streams, and PostgreSQL.

## Table of Contents

1. [High-Level Architecture](#high-level-architecture)
2. [Database Schema](#database-schema)
3. [Notification Envelope Schema](#notification-envelope-schema)
4. [Core FastAPI API](#core-fastapi-api)
5. [Worker Patterns](#worker-patterns)
6. [Retry Strategy & Dead Letter Queue](#retry-strategy--dead-letter-queue)
7. [Dynamic Channels & Runtime Config](#dynamic-channels--runtime-config)
8. [WebSocket Gateway](#websocket-gateway)
9. [Production Deployment](#production-deployment)
10. [Observability & Metrics](#observability--metrics)
11. [Security & Compliance](#security--compliance)
12. [Testing Strategy](#testing-strategy)
13. [Local Development Setup](#local-development-setup)
14. [Scaling Considerations](#scaling-considerations)

## 1. High-Level Architecture

```
Producers (your services: payments, watchlist, admin UI)
        ↓ (HTTP)
 FastAPI Notification API (DO)  <-- primary validation & persistence
        ↓ (writes)                 (Postgres on DO)
 Postgres (notifications table)   (canonical store)
        ↓ (xadd)
 Redis Streams: notifications.stream
        ↓ (consumer groups)
 ┌───────────────────────────────┬─────────────────────────────┐
 │ inapp_consumer_group          │ email_consumer_group        │
 │ - inapp worker(s)             │ - email worker(s)           │
 │ - websocket gateway(s)        │ - uses SendGrid/SMTP       │
 ├───────────────────────────────┼─────────────────────────────┤
 │ push_consumer_group           │ sms_consumer_group          │
 │ - push worker(s) (FCM/APNs)   │ - sms worker(s) (Twilio)    │
 └───────────────────────────────┴─────────────────────────────┘
DLQ: notifications.dlq  (Redis Stream or Postgres table)
Realtime fanout: Redis PUB/SUB user:{user_id}:notifications -> WebSocket gateway -> Browser
```

### Key Architecture Points

- **Redis Streams**: Durable, consumer groups, Pending Entries List (PEL) for reliable queueing
- **Redis Pub/Sub**: Low-latency fan-out to multiple WebSocket gateway instances
- **PostgreSQL**: Source-of-truth for notification history and user preferences
- **Horizontal Scaling**: Run N replicas for each consumer group
- **Dynamic Channels**: Store channel configs in channels table; workers read configs dynamically (cached in Redis)

## 2. Database Schema

Save as initial Alembic migration: `001_create_notifications.sql`

```sql
-- Extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- notifications
CREATE TABLE notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type TEXT NOT NULL,           -- payment, invite, watchlist_change, etc.
  user_id UUID NOT NULL,
  actor_user_id UUID NULL,
  channels TEXT[] NOT NULL,           -- ["inapp","email"]
  payload JSONB NOT NULL,
  template_name TEXT NULL,
  status TEXT NOT NULL DEFAULT 'pending', -- pending, delivered, failed, canceled
  attempts INT NOT NULL DEFAULT 0,
  dedup_key TEXT NULL,
  priority INT DEFAULT 100,
  created_at TIMESTAMPTZ DEFAULT now(),
  delivered_at TIMESTAMPTZ NULL,
  read_at TIMESTAMPTZ NULL
);
CREATE INDEX idx_notifications_user_status ON notifications (user_id, status);
CREATE INDEX idx_notifications_dedup_key ON notifications (dedup_key);

-- channels (dynamic)
CREATE TABLE channels (
  slug TEXT PRIMARY KEY,          -- "email", "sms", "push", "inapp", "webhook"
  name TEXT NOT NULL,
  enabled BOOLEAN DEFAULT TRUE,
  config JSONB DEFAULT '{}'       -- provider information for this channel
);

-- user_channel_prefs
CREATE TABLE user_channel_prefs (
  user_id UUID NOT NULL,
  channel_slug TEXT NOT NULL,
  opted_in BOOLEAN DEFAULT TRUE,
  constraints JSONB DEFAULT '{}',
  PRIMARY KEY (user_id, channel_slug)
);

-- device_tokens for push
CREATE TABLE device_tokens (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  provider TEXT NOT NULL,   -- fcm/apns
  token TEXT NOT NULL,
  platform TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- failed notifications / DLQ
CREATE TABLE notifications_failed (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  notification_id UUID NULL,
  payload JSONB,
  error TEXT,
  failed_at TIMESTAMPTZ DEFAULT now()
);

-- templates
CREATE TABLE templates (
  name TEXT PRIMARY KEY,
  channel_slug TEXT NOT NULL,
  subject TEXT,
  body TEXT,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Seed core channels
INSERT INTO channels (slug, name, enabled, config) VALUES
('inapp','In-App', true, '{}'),
('email','Email', true, '{"provider":"sendgrid"}'),
('sms','SMS', true, '{"provider":"twilio"}'),
('push','Push', true, '{"provider":"fcm"}'),
('webhook','Webhook', true, '{}')
ON CONFLICT (slug) DO NOTHING;
```

### Schema Notes

- Templates stored as DB rows for editability (alternatively use S3 for large templates)
- Secrets (API keys) should NOT be stored in DB — use environment variables or DO Secrets
- `dedup_key` enables idempotent notification creation

## 3. Notification Envelope Schema

All messages on Redis Stream follow this canonical JSON envelope:

```json
{
  "notification_id": "uuid",
  "event_type": "payment",
  "user_id": "uuid-owner",
  "actor_user_id": "uuid-buyer",
  "channels": ["inapp", "email", "sms"],
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

Workers validate this envelope and apply per-channel logic (templating, provider calls).

## 4. Core FastAPI API

Complete minimal FastAPI app (`app/api.py`):

```python
# app/api.py
import os, json, uuid, datetime
from fastapi import FastAPI, Depends, HTTPException, status
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from redis.asyncio import Redis

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/notifications")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
STREAM_NAME = "notifications.stream"

engine = create_async_engine(DATABASE_URL, future=True, echo=False)
AsyncSessionLocal = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
redis = Redis.from_url(REDIS_URL, decode_responses=False)

class NotificationCreate(BaseModel):
    user_id: str
    event_type: str
    channels: List[str]
    payload: Dict[str, Any]
    template_name: Optional[str] = None
    dedup_key: Optional[str] = None
    priority: Optional[int] = 100

app = FastAPI(title="Notification API")

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@app.post("/v1/notifications", status_code=202)
async def create_notification(req: NotificationCreate, db: AsyncSession = Depends(get_db)):
    notification_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat() + "Z"
    
    # Insert to notifications table
    await db.execute(text("""
        INSERT INTO notifications (id, event_type, user_id, channels, payload, template_name, dedup_key, priority, created_at)
        VALUES (:id, :event_type, :user_id, :channels, :payload::jsonb, :template, :dedup_key, :priority, :created_at)
    """), {
        "id": notification_id,
        "event_type": req.event_type,
        "user_id": req.user_id,
        "channels": req.channels,
        "payload": json.dumps(req.payload),
        "template": req.template_name,
        "dedup_key": req.dedup_key,
        "priority": req.priority,
        "created_at": now
    })
    await db.commit()
    
    # Publish to Redis Stream
    msg = req.dict()
    msg.update({"notification_id": notification_id, "created_at": now})
    await redis.xadd(STREAM_NAME, {"data": json.dumps(msg)})
    
    return {"notification_id": notification_id, "status": "queued"}
```

### API Notes

- For production: validate `user_id` existence and permissions
- Add rate limiting and authentication (JWT/OAuth)
- Consider per-channel streams vs single stream with filtering

## 5. Worker Patterns

### General Worker Rules

1. Use `XGROUP CREATE` to create consumer groups
2. Use `XREADGROUP` to read messages (`>` for new messages)
3. After processing, call `XACK`
4. Periodically `XCLAIM` stuck messages in PEL
5. Implement retry logic with backoff and DLQ after max attempts

### 5.1 In-App Worker

Stores/marks delivered + publishes to Redis PUB/SUB for real-time delivery:

```python
# app/workers/inapp_worker.py
import asyncio, json, os, time
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
STREAM = "notifications.stream"
GROUP = "inapp_group"
CONSUMER = os.getenv("CONSUMER_NAME", "inapp-1")

redis = Redis.from_url(REDIS_URL, decode_responses=False)
DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_async_engine(DATABASE_URL)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def ensure_group():
    try:
        await redis.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
    except Exception as e:
        # Group already exists
        pass

async def process_message(msg_id, entry):
    data = json.loads(entry[b"data"].decode())
    user_id = data["user_id"]
    notification_id = data["notification_id"]
    
    # Update notification status in DB
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            UPDATE notifications SET status='delivered', delivered_at=now(), attempts = attempts + 1
            WHERE id = :id
        """), {"id": notification_id})
        await session.commit()
    
    # Publish for real-time delivery
    payload = {
        "id": notification_id,
        "payload": data["payload"],
        "event_type": data.get("event_type")
    }
    await redis.publish(f"user:{user_id}:notifications", json.dumps(payload))

async def consumer_loop():
    await ensure_group()
    while True:
        res = await redis.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=5, block=5000)
        if not res:
            await claim_stale()
            continue
            
        for stream_name, messages in res:
            for msg_id, fields in messages:
                try:
                    await process_message(msg_id, fields)
                    await redis.xack(STREAM, GROUP, msg_id)
                except Exception as e:
                    print("Error processing:", e)

async def claim_stale():
    """Claim messages pending for too long and reprocess"""
    pending = await redis.xpending_range(STREAM, GROUP, min='-', max='+', count=10)
    for p in pending:
        msg_id = p['message_id']
        idle = p['idle']  # milliseconds
        if idle > 60000:  # 60s
            claimed = await redis.xclaim(STREAM, GROUP, CONSUMER, min_idle_time=60000, message_ids=[msg_id])
            for msg_id, fields in claimed:
                try:
                    await process_message(msg_id, fields)
                    await redis.xack(STREAM, GROUP, msg_id)
                except Exception:
                    pass

if __name__ == "__main__":
    asyncio.run(consumer_loop())
```

### 5.2 Email Worker

SendGrid integration example:

```python
# app/workers/email_worker.py
import asyncio, os, json
from redis.asyncio import Redis
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from jinja2 import Template
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
STREAM = "notifications.stream"
GROUP = "email_group"
CONSUMER = os.getenv("CONSUMER_NAME", "email-1")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")

redis = Redis.from_url(REDIS_URL, decode_responses=False)
sg = SendGridAPIClient(SENDGRID_API_KEY)
engine = create_async_engine(os.getenv("DATABASE_URL"))
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def send_email(to_email, subject, html_content):
    message = Mail(
        from_email="noreply@example.com",
        to_emails=to_email,
        subject=subject,
        html_content=html_content
    )
    resp = sg.send(message)
    return resp

async def render_template(template_name: str, context: dict):
    """Fetch template from DB and render with context"""
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            text("SELECT body, subject FROM templates WHERE name = :name"),
            {"name": template_name}
        )
        row = res.fetchone()
        if not row:
            return None, None
        body, subject = row
    
    t = Template(body)
    return t.render(**context), subject

def get_user_email(user_id):
    """TODO: Implement user email lookup"""
    # Join with users table or call user service
    return f"user+{user_id}@example.com"  # Placeholder

async def process_message(msg_id, fields):
    data = json.loads(fields[b"data"].decode())
    
    # Skip if not for email channel
    if "email" not in data.get("channels", []):
        return
        
    user_id = data["user_id"]
    template_name = data.get("template_name") or "default"
    
    # Get user's email
    user_email = get_user_email(user_id)
    
    # Render template
    html, subject = await render_template(template_name, data["payload"])
    if not html:
        return
    
    # Send email (use asyncio.to_thread for sync SendGrid client)
    resp = await asyncio.to_thread(send_email, user_email, subject, html)
    
    # Update attempt counter
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            UPDATE notifications SET attempts = attempts + 1 WHERE id = :id
        """), {"id": data["notification_id"]})
        await session.commit()

# Consumer loop pattern same as inapp_worker
```

### 5.3 SMS Worker (Twilio)

Similar pattern using Twilio REST API with async HTTP client (`httpx`).

### 5.4 Push Worker (FCM)

Use FCM HTTP v1 API. Batch device tokens and handle invalid token cleanup.

## 6. Retry Strategy & Dead Letter Queue

### 6.1 Consumer Groups + PEL

- Each consumer group maintains a Pending Entries List (PEL)
- Failed `XACK` operations keep messages in PEL
- Workers periodically use `XPENDING`/`XPENDING_RANGE` to find idle messages
- Use `XCLAIM` to reprocess stuck messages

### 6.2 Attempt Counter

```python
# On each processing attempt
async def process_with_retry(notification_id, max_attempts=5):
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("""
            UPDATE notifications 
            SET attempts = attempts + 1 
            WHERE id = :id 
            RETURNING attempts
        """), {"id": notification_id})
        attempts = result.fetchone()[0]
        
        if attempts > max_attempts:
            # Move to DLQ
            await session.execute(text("""
                INSERT INTO notifications_failed (notification_id, payload, error, failed_at)
                SELECT id, payload, 'Max attempts exceeded', now()
                FROM notifications WHERE id = :id
            """), {"id": notification_id})
            
        await session.commit()
        return attempts <= max_attempts
```

### 6.3 Delayed Retries (Exponential Backoff)

Redis Streams lacks native delayed delivery. Implementation options:

#### Option 1: Retry Stream + Scheduler

```python
# Worker fails -> add to retry with delay
backoff_seconds = 2 ** attempts
deliver_at = time.time() + backoff_seconds
await redis.zadd("notifications:retry", {notification_id: deliver_at})

# Scheduler process (runs every second)
async def retry_scheduler():
    while True:
        now = time.time()
        # Get due items
        due_items = await redis.zrangebyscore("notifications:retry", 0, now, withscores=True)
        for item, score in due_items:
            # Move back to main stream
            await redis.xadd(STREAM_NAME, {"data": item})
            await redis.zrem("notifications:retry", item)
        await asyncio.sleep(1)
```

#### Option 2: Sorted Set with TTL

Use `ZADD` with score = deliver_at timestamp. Scheduler polls and moves due items back to main stream.

## 7. Dynamic Channels & Runtime Config

### Channel Management

Workers cache channel configuration from database in Redis:

```python
async def load_channel_config():
    """Load and cache channel configuration"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT slug, config, enabled FROM channels"))
        channels = {row.slug: {"config": row.config, "enabled": row.enabled} 
                   for row in result}
    
    # Cache in Redis with TTL
    await redis.setex("channels_cache", 60, json.dumps(channels))
    return channels

async def get_channel_config(channel_slug):
    """Get channel config from cache or DB"""
    cached = await redis.get("channels_cache")
    if cached:
        channels = json.loads(cached)
        return channels.get(channel_slug)
    
    # Fallback to DB
    channels = await load_channel_config()
    return channels.get(channel_slug)
```

### Admin API Endpoints

```python
@app.get("/v1/channels")
async def list_channels(db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("SELECT * FROM channels"))
    return [dict(row) for row in result]

@app.post("/v1/channels/{slug}/toggle")
async def toggle_channel(slug: str, enabled: bool, db: AsyncSession = Depends(get_db)):
    await db.execute(text("""
        UPDATE channels SET enabled = :enabled WHERE slug = :slug
    """), {"enabled": enabled, "slug": slug})
    await db.commit()
    
    # Notify workers to refresh config
    await redis.publish("channels:changed", slug)
    return {"status": "updated"}
```

### Generic Webhook Channel

```json
{
  "slug": "webhook",
  "config": {
    "url": "https://example.com/webhook",
    "method": "POST",
    "headers": {"Authorization": "Bearer token"}
  }
}
```

Webhook worker reads config and makes HTTP calls, enabling new channels without code changes.

## 8. WebSocket Gateway

Real-time notification delivery via WebSockets:

```python
# app/ws_gateway.py
import os, json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
import asyncio

app = FastAPI()
redis = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379"), decode_responses=False)

# Store connected sockets per user (single instance)
# For multiple instances, each receives messages via Redis Pub/Sub
connected = {}

@app.websocket("/ws/{user_id}")
async def ws_endpoint(websocket: WebSocket, user_id: str):
    await websocket.accept()
    channel = f"user:{user_id}:notifications"
    
    # Subscribe to Redis Pub/Sub
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    
    if user_id not in connected:
        connected[user_id] = []
    connected[user_id].append(websocket)
    
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=10)
            if msg and msg['data']:
                data = msg['data']
                if isinstance(data, bytes):
                    data = data.decode()
                await websocket.send_text(data)
            await asyncio.sleep(0.01)
    except WebSocketDisconnect:
        connected[user_id].remove(websocket)
    finally:
        await pubsub.unsubscribe(channel)
```

### Scaling Notes

- Run multiple gateway instances behind load balancer
- Each instance receives all Redis Pub/Sub messages
- Only send to locally connected WebSockets
- Consider JWT authentication on WebSocket connect

## 9. Production Deployment

### DigitalOcean App Platform (Recommended)

1. **Build Docker Images**:
   - `api` (FastAPI notification API)
   - `worker-inapp` (In-app notification worker)
   - `worker-email` (Email worker)
   - `ws-gateway` (WebSocket gateway)

2. **Deploy Services**:
   - API and WebSocket gateway as web services (load balanced)
   - Workers as background services (fixed container count)

3. **Managed Services**:
   - DO Managed PostgreSQL (private network)
   - DO Managed Redis

4. **Environment Variables**:
   ```bash
   DATABASE_URL=postgresql+asyncpg://user:pass@db-host/notifications
   REDIS_URL=redis://redis-host:6379
   SENDGRID_API_KEY=your-sendgrid-key
   ```

### Alternative: Droplets + systemd

- Run containers on Droplets
- Use systemd for service management
- Manual orchestration

### Alternative: DO Kubernetes

- More operational overhead
- Better for complex multi-service deployments
- Auto-scaling capabilities

## 10. Observability & Metrics

### Key Metrics

```python
from prometheus_client import Counter, Histogram, Gauge

# Counters
notifications_created = Counter('notifications_created_total', 'Total notifications created', ['channel'])
notifications_processed = Counter('notifications_processed_total', 'Total notifications processed', ['channel', 'status'])
notifications_failed = Counter('notifications_failed_total', 'Total notification failures', ['channel', 'error_type'])

# Gauges
queue_length = Gauge('notifications_queue_length', 'Current queue length')
pending_messages = Gauge('notifications_pending_messages', 'Pending messages in consumer groups', ['group'])

# Histograms
processing_time = Histogram('notification_processing_seconds', 'Time to process notification', ['channel'])
```

### Redis Metrics

```python
async def collect_redis_metrics():
    # Queue length
    length = await redis.xlen("notifications.stream")
    queue_length.set(length)
    
    # Pending messages per group
    for group in ["inapp_group", "email_group", "sms_group", "push_group"]:
        try:
            pending = await redis.xpending("notifications.stream", group)
            pending_messages.labels(group=group).set(pending['pending'])
        except:
            pass
```

### Structured Logging

```python
import structlog

logger = structlog.get_logger()

# In worker
logger.info("notification_processed", 
           notification_id=notification_id,
           user_id=user_id,
           channel=channel,
           attempts=attempts,
           processing_time_ms=processing_time)
```

### Alerts

- DLQ growth > X notifications in 1 hour
- Pending messages > threshold for > 5 minutes
- Delivery error rate > 5% for any channel
- Queue length > 10,000 messages

## 11. Security & Compliance

### Authentication & Authorization

```python
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer
import jwt

security = HTTPBearer()

async def verify_token(token: str = Depends(security)):
    try:
        payload = jwt.decode(token.credentials, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.post("/v1/notifications")
async def create_notification(req: NotificationCreate, 
                            user: dict = Depends(verify_token),
                            db: AsyncSession = Depends(get_db)):
    # Verify user can send notifications for user_id
    if not can_send_notification(user, req.user_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    # ... rest of implementation
```

### Security Checklist

- ✅ API authentication (JWT/mTLS)
- ✅ Secrets management (DO App Platform secrets)
- ✅ TLS for all traffic
- ✅ Rate limiting per producer and per user
- ✅ Structured logging (redact PII)
- ✅ Input validation and sanitization
- ✅ Network security (private DB/Redis)

### GDPR Compliance

```python
@app.delete("/v1/notifications/user/{user_id}")
async def delete_user_notifications(user_id: str, db: AsyncSession = Depends(get_db)):
    """GDPR: Delete all notifications for a user"""
    await db.execute(text("""
        DELETE FROM notifications WHERE user_id = :user_id
    """), {"user_id": user_id})
    
    await db.execute(text("""
        DELETE FROM user_channel_prefs WHERE user_id = :user_id
    """), {"user_id": user_id})
    
    await db.execute(text("""
        DELETE FROM device_tokens WHERE user_id = :user_id
    """), {"user_id": user_id})
    
    await db.commit()
    return {"status": "deleted"}
```

### Email Deliverability

- Configure SPF, DKIM, DMARC for sending domain
- Use dedicated IP for high-volume sending
- Monitor bounce and complaint rates
- Implement suppression lists

## 12. Testing Strategy

### Unit Tests

```python
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_notification_creation():
    mock_db = AsyncMock()
    mock_redis = AsyncMock()
    
    with patch('app.api.redis', mock_redis), \
         patch('app.api.get_db', return_value=mock_db):
        
        response = await create_notification(NotificationCreate(
            user_id="user-123",
            event_type="payment",
            channels=["email"],
            payload={"amount": 100}
        ), mock_db)
        
        assert response["status"] == "queued"
        mock_redis.xadd.assert_called_once()

@pytest.mark.asyncio
async def test_template_rendering():
    template_body = "Hello {{name}}, your payment of ${{amount}} was processed."
    context = {"name": "John", "amount": "100.00"}
    
    result = render_template_string(template_body, context)
    assert result == "Hello John, your payment of $100.00 was processed."
```

### Integration Tests

```python
@pytest.mark.asyncio
async def test_end_to_end_notification():
    # Use testcontainers for Redis and PostgreSQL
    async with TestClient() as client:
        # Create notification
        response = await client.post("/v1/notifications", json={
            "user_id": "test-user",
            "event_type": "test",
            "channels": ["inapp"],
            "payload": {"message": "test notification"}
        })
        
        notification_id = response.json()["notification_id"]
        
        # Verify in database
        async with get_test_db() as db:
            result = await db.execute(text(
                "SELECT status FROM notifications WHERE id = :id"
            ), {"id": notification_id})
            status = result.fetchone()[0]
            assert status == "pending"
        
        # Simulate worker processing
        # Verify Redis Stream message
        # Check final status
```

### Load Testing

k6 script for load testing:

```javascript
import http from 'k6/http';
import { check } from 'k6';

export let options = {
  stages: [
    { duration: '2m', target: 100 },
    { duration: '5m', target: 100 },
    { duration: '2m', target: 200 },
    { duration: '5m', target: 200 },
    { duration: '2m', target: 0 },
  ],
};

export default function () {
  let payload = JSON.stringify({
    user_id: `user-${Math.floor(Math.random() * 1000)}`,
    event_type: "load_test",
    channels: ["inapp", "email"],
    payload: { 
      amount: Math.random() * 100,
      message: "Load test notification"
    }
  });
  
  let response = http.post('http://localhost:8000/v1/notifications', payload, {
    headers: { 'Content-Type': 'application/json' }
  });
  
  check(response, {
    'status is 202': (r) => r.status === 202,
    'response time < 500ms': (r) => r.timings.duration < 500,
  });
}
```

## 13. Local Development Setup

### docker-compose.yml

```yaml
version: '3.9'
services:
  api:
    build: .
    command: uvicorn app.api:app --host 0.0.0.0 --port 8000 --reload
    env_file: .env
    ports:
      - "8000:8000"
    depends_on:
      - db
      - redis
    volumes:
      - .:/app

  worker-inapp:
    build: .
    command: python app/workers/inapp_worker.py
    env_file: .env
    depends_on:
      - redis
      - db
    volumes:
      - .:/app

  worker-email:
    build: .
    command: python app/workers/email_worker.py
    env_file: .env
    depends_on:
      - redis
      - db
    volumes:
      - .:/app

  ws-gateway:
    build: .
    command: uvicorn app.ws_gateway:app --host 0.0.0.0 --port 8001
    env_file: .env
    ports:
      - "8001:8001"
    depends_on:
      - redis

  db:
    image: postgres:15
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: notifications
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

### .env File

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/notifications
REDIS_URL=redis://localhost:6379
SENDGRID_API_KEY=your-sendgrid-key-here
TWILIO_ACCOUNT_SID=your-twilio-sid
TWILIO_AUTH_TOKEN=your-twilio-token
FCM_SERVER_KEY=your-fcm-key
```

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Quick Start

1. **Clone and Setup**:
   ```bash
   git clone <repo>
   cd notification-service
   cp .env.example .env  # Edit with your values
   ```

2. **Start Services**:
   ```bash
   docker-compose up --build
   ```

3. **Initialize Database**:
   ```bash
   docker-compose exec db psql -U postgres -d notifications -f /docker-entrypoint-initdb.d/001_create_notifications.sql
   ```

4. **Test Notification**:
   ```bash
   curl -X POST http://localhost:8000/v1/notifications \
     -H "Content-Type: application/json" \
     -d '{
       "user_id": "user-123",
       "event_type": "payment",
       "channels": ["inapp", "email"],
       "payload": {"amount": 12.50, "product": "Premium Plan"}
     }'
   ```

5. **Watch Logs**:
   ```bash
   docker-compose logs -f worker-inapp worker-email
   ```

## 14. Scaling Considerations

### Horizontal Scaling Patterns

#### Hot-User Sharding

For users generating high notification volumes:

```python
def get_stream_name(user_id: str, num_shards: int = 4) -> str:
    shard = hash(user_id) % num_shards
    return f"notifications.stream.{shard}"

# In API
stream_name = get_stream_name(req.user_id)
await redis.xadd(stream_name, {"data": json.dumps(msg)})

# Workers read from all shards
STREAMS = [f"notifications.stream.{i}" for i in range(4)]
```

#### Per-Channel Streams

For extremely high volume per channel:

```python
# Separate streams per channel
STREAM_MAPPING = {
    "email": "notifications.email.stream",
    "sms": "notifications.sms.stream",
    "push": "notifications.push.stream",
    "inapp": "notifications.inapp.stream"
}

# API publishes to multiple streams
for channel in req.channels:
    stream_name = STREAM_MAPPING.get(channel, "notifications.default.stream")
    await redis.xadd(stream_name, {"data": json.dumps(msg)})
```

### Provider-Specific Optimizations

#### Email Batching

```python
async def batch_email_worker():
    """Send emails in batches for better throughput"""
    batch = []
    batch_size = 100
    
    while True:
        # Collect messages
        res = await redis.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=batch_size, block=1000)
        
        if res:
            for stream_name, messages in res:
                batch.extend(messages)
        
        if len(batch) >= batch_size or (batch and not res):
            await send_email_batch(batch)
            # ACK all messages in batch
            for msg_id, _ in batch:
                await redis.xack(STREAM, GROUP, msg_id)
            batch = []
```

#### Push Notification Batching

```python
async def batch_push_notifications(notifications):
    """Send push notifications in batches to FCM"""
    # Group by app/project
    batches = {}
    for notif in notifications:
        app_id = get_app_id(notif['user_id'])
        if app_id not in batches:
            batches[app_id] = []
        batches[app_id].append(notif)
    
    # Send each batch
    for app_id, batch in batches.items():
        device_tokens = [get_device_token(n['user_id']) for n in batch]
        await fcm_client.send_multicast(device_tokens, batch[0]['payload'])
```

### Auto-Scaling

#### Worker Auto-Scaling

Monitor queue metrics and scale workers:

```python
async def auto_scale_workers():
    """Scale workers based on queue depth"""
    queue_length = await redis.xlen("notifications.stream")
    pending_count = len(await redis.xpending("notifications.stream", "email_group"))
    
    # Scale up if queue is backing up
    if queue_length > 1000 or pending_count > 500:
        scale_workers("email_worker", replicas=min(10, current_replicas * 2))
    
    # Scale down if queue is empty
    elif queue_length < 100 and pending_count < 50:
        scale_workers("email_worker", replicas=max(1, current_replicas // 2))
```

#### Database Connection Pooling

```python
# Use connection pooling for high concurrency
from sqlalchemy.pool import QueuePool

engine = create_async_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=20,
    max_overflow=30,
    pool_pre_ping=True,
    pool_recycle=3600
)
```

### Performance Monitoring

- Monitor Redis memory usage and eviction policies
- Track database connection pool utilization
- Monitor provider API rate limits and quotas
- Set up alerts for queue backup scenarios
- Use distributed tracing for end-to-end latency

---

This architecture provides a solid foundation for a scalable, reliable notification service that can handle millions of notifications per day while maintaining good performance and reliability characteristics.