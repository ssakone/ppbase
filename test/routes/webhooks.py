"""Webhook receiver routes module.

Demonstrates:
  - Reading raw request body
  - HMAC signature verification pattern
  - Async background tasks from routes

Loaded via:
    pb.load_hooks("routes.webhooks:setup")
"""

from __future__ import annotations


def setup(pb) -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    webhooks = pb.group("/api/webhooks")

    @webhooks.post("/generic")
    async def generic_webhook(request: Request):
        """Accept any JSON webhook and log the event type."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"status": 400, "message": "Invalid JSON body.", "data": {}},
            )

        event_type = payload.get("type") or payload.get("event") or "unknown"
        source = request.headers.get("x-source", "unknown")
        print(f"[webhook] received event={event_type} from={source}")

        return {"received": True, "event": event_type}

    @webhooks.post("/github")
    async def github_webhook(request: Request):
        """GitHub-style webhook with signature verification."""
        import hashlib
        import hmac

        WEBHOOK_SECRET = "dev-secret"   # in production: read from env

        body = await request.body()
        sig_header = request.headers.get("x-hub-signature-256", "")

        if sig_header:
            expected = "sha256=" + hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                return JSONResponse(
                    status_code=401,
                    content={"status": 401, "message": "Invalid webhook signature.", "data": {}},
                )

        event_type = request.headers.get("x-github-event", "unknown")
        print(f"[webhook/github] event={event_type} bytes={len(body)}")

        return {"received": True, "event": event_type}

    @webhooks.post("/stripe")
    async def stripe_webhook(request: Request):
        """Stripe-style webhook (Stripe-Signature header)."""
        body = await request.body()
        sig = request.headers.get("stripe-signature", "")
        print(f"[webhook/stripe] sig={sig[:20]}… bytes={len(body)}")
        # In production: stripe.Webhook.construct_event(body, sig, secret)
        return {"received": True}
