# 🧩 GCP Pub/Sub Notification System Implementation Guide

This comprehensive guide walks you through building a scalable notification system using Google Cloud Pub/Sub.

## System Overview

### Components
- **FastAPI app** - Your main application system
- **GCP Pub/Sub Topics + Subscriptions** - Message broker infrastructure
- **Cloud Functions or Background Workers** - Event processors
- **PostgreSQL** - Notification storage
- **Firebase** (optional) - Push notifications
- **Email/SMS providers** - External notification delivery (SendGrid, Twilio)

---

## ⚙️ Step 1: GCP Setup

### 1️⃣ Enable APIs
In your GCP console:
```bash
gcloud services enable pubsub.googleapis.com
```

### 2️⃣ Create Topics
Each channel or event category gets its own topic (can be made dynamic later):
```bash
gcloud pubsub topics create payments
gcloud pubsub topics create invites
gcloud pubsub topics create email
gcloud pubsub topics create sms
gcloud pubsub topics create inapp
gcloud pubsub topics create watchlist
gcloud pubsub topics create asset-price
gcloud pubsub topics create funds
```
> **Note:** Later you can implement a "channel registry" in your database to make this dynamic.

### 3️⃣ Create Subscriptions
Each consumer (Cloud Function, worker, or service) gets a subscription:
```bash
gcloud pubsub subscriptions create payments-sub --topic=payments
gcloud pubsub subscriptions create invites-sub --topic=invites
gcloud pubsub subscriptions create email-sub --topic=email
# ... continue for other topics
```

### 4️⃣ Create Service Account for Access
```bash
gcloud iam service-accounts create notification-publisher

gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:notification-publisher@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher"

gcloud iam service-accounts keys create key.json \
  --iam-account=notification-publisher@<PROJECT_ID>.iam.gserviceaccount.com
```
> **Security Note:** Store `key.json` securely in your FastAPI environment.

---

## ⚙️ Step 2: FastAPI Publishes Messages

### Install Dependencies
```bash
pip install fastapi uvicorn google-cloud-pubsub pydantic
```

### 📁 Create Pub/Sub Client (`app/pubsub_client.py`)
```python
from google.cloud import pubsub_v1
import json
import os

PROJECT_ID = os.getenv("GCP_PROJECT_ID")
publisher = pubsub_v1.PublisherClient()
topic_cache = {}

def get_topic_path(topic_name: str):
    if topic_name not in topic_cache:
        topic_cache[topic_name] = publisher.topic_path(PROJECT_ID, topic_name)
    return topic_cache[topic_name]

def publish_event(topic: str, payload: dict):
    topic_path = get_topic_path(topic)
    message = json.dumps(payload).encode("utf-8")
    future = publisher.publish(topic_path, data=message)
    return future.result()
```

### 📁 Main Application (`app/main.py`)
```python
from fastapi import FastAPI
from pydantic import BaseModel
from app.pubsub_client import publish_event

app = FastAPI()

class NotificationPayload(BaseModel):
    channel: str
    user_id: int
    title: str
    message: str
    metadata: dict = {}

@app.post("/notify")
def send_notification(payload: NotificationPayload):
    # Publish to Pub/Sub
    publish_event(payload.channel, payload.dict())
    return {"status": "queued", "channel": payload.channel}
```

### ✅ Example Request
```bash
curl -X POST http://localhost:8000/notify \
-H "Content-Type: application/json" \
-d '{
  "channel": "payments",
  "user_id": 123,
  "title": "Payment Successful",
  "message": "Your payment of $29.99 has been received.",
  "metadata": {"txn_id": "TXN12345"}
}'
```

---

## ⚙️ Step 3: Cloud Function Consumer

Each Cloud Function listens to its topic and processes notifications.

### Example: Payment Handler (`payment_handler/main.py`)
```python
import json
import base64
from google.cloud import pubsub_v1
import requests

def process_payment_event(event, context):
    if 'data' in event:
        message = json.loads(base64.b64decode(event['data']).decode('utf-8'))
        user_id = message['user_id']
        txn = message['metadata']['txn_id']
        print(f"Processing payment notification for {user_id}: {txn}")

        # Call your FastAPI internal endpoint
        requests.post("https://your-fastapi-domain.com/internal/notifications", json={
            "user_id": user_id,
            "type": "payment",
            "data": message
        })
```

### Deploy Cloud Function
```bash
gcloud functions deploy process_payment_event \
  --runtime python311 \
  --trigger-topic payments \
  --region europe-west1 \
  --service-account notification-publisher@<PROJECT_ID>.iam.gserviceaccount.com
```

---

## ⚙️ Step 4: Internal FastAPI Route to Store Notifications

```python
@app.post("/internal/notifications")
def internal_store_notification(payload: NotificationPayload):
    # Save to PostgreSQL
    db.execute(
        "INSERT INTO notifications (user_id, channel, title, message, metadata) VALUES (%s, %s, %s, %s, %s)",
        (payload.user_id, payload.channel, payload.title, payload.message, json.dumps(payload.metadata))
    )
    return {"stored": True}
```

---

## ⚙️ Step 5: Optional Real-time WebSocket Updates

After saving to the database, you can publish to a Redis Pub/Sub channel or Socket.IO server for live updates in web and mobile applications.

---

## ⚙️ Step 6: Mobile & Web App Consumption

### Mobile App
Use **Firebase Cloud Messaging (FCM)** to receive push notifications when `channel == "push"`.

### Web App
Use **WebSockets** or **Server-Sent Events (SSE)** to show real-time updates.

### In-App Feed
Fetch from `/notifications?user_id=123` API endpoint.

---

## ⚙️ Step 7: Dynamic Channels

Store available channels in a `notification_channels` table:

| id | name     | topic_name | active |
|----|----------|------------|--------|
| 1  | payments | payments   | true   |
| 2  | invites  | invites    | true   |

The `/notify` endpoint can check this table before publishing.

---

## ⚙️ Step 8: Efficiency & Cost Comparison

| Feature      | GCP Pub/Sub        | RabbitMQ           | Redis Streams      |
|--------------|--------------------|--------------------|-------------------|
| Reliability  | ✅ At-least-once   | ✅                 | ⚠️ Needs config   |
| Scalability  | ✅ Global          | ✅                 | ⚠️ Limited by memory |
| Latency      | ⚡ ~100ms          | ⚡ ~50ms           | ⚡ ~1–5ms         |
| Persistence  | ✅ Durable         | ✅ Durable         | ⚠️ Optional       |
| Integration  | ✅ Cloud Functions, Cloud Run | 🧰 Manual setup | 🧰 Manual setup |
| Management   | ✅ Managed         | ❌ Self-hosted     | ❌ Self-hosted    |
| Cost         | 💰 Pay per message | 💰 Infra + Ops    | 💰 Infra + Memory |

### Recommendation
✅ **GCP Pub/Sub** is the best choice if you want reliability with no DevOps overhead. 
It's slightly slower but extremely scalable, fault-tolerant, and requires minimal maintenance.

---

## ⚙️ Step 9: Local Development

Use the Pub/Sub Emulator for local testing:
```bash
gcloud beta emulators pubsub start
export PUBSUB_EMULATOR_HOST=localhost:8085
```

---

## ⚙️ Step 10: Security & IAM Best Practices

- **Use service accounts** for each Cloud Function
- **Assign least privilege** permissions:
  - `roles/pubsub.subscriber` for consumers
  - `roles/pubsub.publisher` for publishers
- **Store service account JSON** securely in:
  - DigitalOcean Secrets Manager
  - HashiCorp Vault
  - Or your preferred secrets management solution
