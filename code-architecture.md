# Notification Service Architecture Options

This document outlines two main architectural approaches for implementing a notification service using different technologies and cloud platforms.

## 🧩 Option 1 — DigitalOcean + Redis Streams (Most Natural for You)

### Directory Structure
```
fastapi-redis-notify/
├── app/
│   ├── main.py
│   ├── db.py
│   ├── models.py
│   ├── schemas.py
│   ├── redis_client.py
│   └── worker_inapp.py
├── docker-compose.yml
├── requirements.txt
└── README.md
```

### Configuration Files

#### docker-compose.yml
```yaml
version: "3.9"
services:
  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    volumes:
      - .:/code
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/notifications
      - REDIS_URL=redis://redis:6379
    ports:
      - "8000:8000"
    depends_on:
      - db
      - redis

  worker:
    build: .
    command: python app/worker_inapp.py
    volumes:
      - .:/code
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/notifications
      - REDIS_URL=redis://redis:6379
    depends_on:
      - db
      - redis

  db:
    image: postgres:15
    environment:
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: notifications
    ports:
      - "5432:5432"

  redis:
    image: redis:7
    ports:
      - "6379:6379"
```

#### requirements.txt
```txt
fastapi
uvicorn
sqlalchemy[asyncio]
asyncpg
redis
pydantic
```

### Application Code

#### app/main.py
```python
from fastapi import FastAPI
from app.schemas import NotificationCreate
from app.redis_client import redis, STREAM_NAME
import json, uuid

app = FastAPI(title="Notification API (Redis Streams)")

@app.post("/v1/notifications")
async def create_notification(req: NotificationCreate):
    notification_id = str(uuid.uuid4())
    msg = req.model_dump()
    msg["notification_id"] = notification_id
    await redis.xadd(STREAM_NAME, {"data": json.dumps(msg)})
    return {"notification_id": notification_id, "status": "queued"}
```

#### app/schemas.py
```python
from pydantic import BaseModel
from typing import List, Dict, Any, Optional

class NotificationCreate(BaseModel):
    user_id: str
    event_type: str
    channels: List[str]
    payload: Dict[str, Any]
    dedup_key: Optional[str] = None
```

#### app/redis_client.py
```python
import asyncio
from redis.asyncio import Redis

STREAM_NAME = "notifications.stream"
redis = Redis.from_url("redis://redis:6379")
```

#### app/worker_inapp.py
```python
import asyncio, json
from app.redis_client import redis, STREAM_NAME

GROUP = "inapp_group"
CONSUMER = "worker-1"

async def main():
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP, id="$", mkstream=True)
    except Exception:
        pass  # already exists
    while True:
        res = await redis.xreadgroup(GROUP, CONSUMER, {STREAM_NAME: ">"}, count=5, block=10000)
        for stream, messages in res or []:
            for msg_id, fields in messages:
                data = json.loads(fields[b"data"])
                print("📨 IN-APP Notification →", data)
                await redis.xack(STREAM_NAME, GROUP, msg_id)

if __name__ == "__main__":
    asyncio.run(main())
```

### Running the Application

1. **Start the services:**
   ```bash
   docker-compose up
   ```

2. **Test with a POST request:**
   ```bash
   curl -X POST http://localhost:8000/v1/notifications \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"u1","event_type":"payment","channels":["inapp"],"payload":{"amount":10}}'
   ```

3. **Expected behavior:**
   - Worker prints the event
   - You can now extend to email/sms/push workers easily

---

## ☁️ Option 2 — GCP Pub/Sub + Cloud Functions (Serverless)

### Architecture Flow
FastAPI (on DO) → Pub/Sub topic notifications → Cloud Function subscribers (email, sms, etc.)

Each function reads JSON, sends notification, and optionally calls back your API or DB.

### FastAPI Publisher (on DigitalOcean)

#### main.py
```python
from fastapi import FastAPI
from google.cloud import pubsub_v1
import os, json, uuid, asyncio

PROJECT_ID = os.getenv("GCP_PROJECT")
TOPIC_ID = "notifications"
publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, TOPIC_ID)

app = FastAPI(title="Notification API (GCP Pub/Sub)")

@app.post("/v1/notifications")
async def create_notification(payload: dict):
    payload["notification_id"] = str(uuid.uuid4())
    data = json.dumps(payload).encode("utf-8")
    future = publisher.publish(topic_path, data, event_type=payload.get("event_type","unknown"))
    msg_id = future.result(timeout=10)
    return {"notification_id": payload["notification_id"], "pubsub_msg_id": msg_id}
```

#### requirements.txt
```txt
fastapi
uvicorn
google-cloud-pubsub
```

### Authentication Setup

Authenticate on DigitalOcean with a service account key having `roles/pubsub.publisher`:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export GCP_PROJECT=my-gcp-project
```

### Cloud Function Example (Python 3.11)

#### main.py
```python
import json
from google.cloud import pubsub_v1

def notify_email(event, context):
    msg = json.loads(base64.b64decode(event['data']).decode())
    print(f"📧 Email notification → {msg}")
    # TODO: send via SendGrid/Twilio/FCM
```

### Deployment Commands

#### Deploy Cloud Function:
```bash
gcloud functions deploy notify_email \
  --runtime python311 \
  --trigger-topic notifications \
  --entry-point notify_email \
  --region europe-west1
```

#### Pub/Sub Setup:
```bash
gcloud pubsub topics create notifications
gcloud pubsub subscriptions create email-sub --topic=notifications
```

Add more functions (`notify_sms`, `notify_push`, etc.) with filtering or use attribute-based subscriptions.

### Observability
- Use GCP Cloud Logging
- Dead Letter Queues (DLQs)
- Built-in metrics with Pub/Sub
- No queue server or Redis maintenance required

---

## ⚖️ Efficiency & Trade-offs Comparison

| Feature | DO + Redis Streams | GCP Pub/Sub + Functions |
|---------|-------------------|-------------------------|
| **Latency** | ~1–5 ms in-cluster | 30–100 ms (cross-cloud + cold starts) |
| **Throughput** | Very high (<1 ms per msg) | High (managed), may throttle after ~10K msg/s |
| **Scalability** | Manual (spawn workers) | Auto (functions scale by load) |
| **Ops Effort** | You manage Redis & workers | Minimal—GCP manages infra |
| **Cost** | Redis + VM cost | Pay per invocation |
| **Cross-cloud complexity** | None (same DO network) | Yes (auth, egress) |
| **Best for** | Real-time & low-latency | Sporadic or bursty workloads |

## Recommendations

- **Choose Option 1 (Redis Streams)** if you need:
  - Ultra-low latency
  - High throughput
  - Simple deployment within DigitalOcean
  - Full control over the infrastructure

- **Choose Option 2 (GCP Pub/Sub)** if you need:
  - Minimal operational overhead
  - Auto-scaling capabilities
  - Serverless architecture
  - Built-in reliability and monitoring
