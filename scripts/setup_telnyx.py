"""Point a Telnyx number at this voice agent.

Idempotent: creates (or updates) a Call Control application whose webhook is our
tunnel URL, then assigns the configured phone number to it. Requires PUBLIC_URL
and TELNYX_API in the environment.
"""
from __future__ import annotations

import sys

import httpx

from config import settings

_BASE = "https://api.telnyx.com/v2"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.telnyx_api_key}",
        "Content-Type": "application/json",
    }


def _client() -> httpx.Client:
    return httpx.Client(base_url=_BASE, headers=_headers(), timeout=20)


def find_number_id(client: httpx.Client, number: str) -> str:
    resp = client.get("/phone_numbers", params={"filter[phone_number]": number})
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        raise SystemExit(f"Number {number} not found on this Telnyx account")
    return data[0]["id"]


def upsert_app(client: httpx.Client) -> str:
    """Return the Call Control application id (connection id) for our agent."""
    resp = client.get("/call_control_applications", params={"page[size]": 100})
    resp.raise_for_status()
    for app in resp.json().get("data", []):
        if app.get("application_name") == settings.call_control_app_name:
            client.patch(
                f"/call_control_applications/{app['id']}",
                json={"webhook_event_url": settings.webhook_url},
            ).raise_for_status()
            print(f"updated app {app['id']} webhook -> {settings.webhook_url}")
            return app["id"]

    create = client.post(
        "/call_control_applications",
        json={
            "application_name": settings.call_control_app_name,
            "webhook_event_url": settings.webhook_url,
        },
    )
    create.raise_for_status()
    app_id = create.json()["data"]["id"]
    print(f"created app {app_id} webhook -> {settings.webhook_url}")
    return app_id


def assign_number(client: httpx.Client, number_id: str, app_id: str) -> None:
    client.patch(
        f"/phone_numbers/{number_id}", json={"connection_id": app_id}
    ).raise_for_status()


def main() -> None:
    if not settings.public_url:
        raise SystemExit("PUBLIC_URL not set (start the tunnel first)")
    with _client() as client:
        number_id = find_number_id(client, settings.agent_number)
        app_id = upsert_app(client)
        assign_number(client, number_id, app_id)
    print("\n=== Telnyx ready ===")
    print(f"  Call this number to reach Aria:  {settings.agent_number}")
    print(f"  Webhook:  {settings.webhook_url}")
    print(f"  Media WS: {settings.media_ws_url}")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print(f"Telnyx API error: {exc.response.status_code} {exc.response.text}")
        sys.exit(1)
