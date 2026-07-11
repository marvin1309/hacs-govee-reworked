"""Govee OpenAPI v2 scene/snapshot helper.

The bundled ``govee-api-laggat`` library talks to the *old* Govee Developer
API (v1), which only supports on/off, brightness, color and color temperature.

Dynamic scenes, DIY scenes and user "snapshots" (the reliable way to switch a
TV backlight such as the H605C into video / DreamView mode) are only exposed by
the *new* Govee OpenAPI v2 at ``https://openapi.api.govee.com``.

This module is a tiny, self-contained async client for exactly that: it lists
the available effects for a device and sends the control call to activate one.
The same Govee API key used by the main integration works here as well.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://openapi.api.govee.com"
DEVICES_URL = f"{BASE_URL}/router/api/v1/user/devices"
SCENES_URL = f"{BASE_URL}/router/api/v1/device/scenes"
DIY_SCENES_URL = f"{BASE_URL}/router/api/v1/device/diy-scenes"
CONTROL_URL = f"{BASE_URL}/router/api/v1/device/control"

_TIMEOUT = aiohttp.ClientTimeout(total=20)

# capability type used for all dynamic scene / snapshot / diy control calls
DYNAMIC_SCENE = "devices.capabilities.dynamic_scene"


def _headers(api_key: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Govee-API-Key": api_key}


def _payload(sku: str, device: str) -> dict[str, Any]:
    return {"requestId": str(uuid.uuid4()), "payload": {"sku": sku, "device": device}}


async def _post(
    session: aiohttp.ClientSession, url: str, api_key: str, body: dict[str, Any]
) -> dict[str, Any] | None:
    """POST helper returning parsed JSON payload, or None on failure."""
    try:
        async with session.post(
            url, json=body, headers=_headers(api_key), timeout=_TIMEOUT
        ) as resp:
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as ex:  # network / timeout
        _LOGGER.debug("Govee v2 request to %s failed: %s", url, ex)
        return None
    except ValueError as ex:  # invalid JSON
        _LOGGER.debug("Govee v2 response from %s was not JSON: %s", url, ex)
        return None

    if not isinstance(data, dict):
        return None
    if data.get("code") not in (200, None):
        _LOGGER.debug(
            "Govee v2 request to %s returned code %s: %s",
            url,
            data.get("code"),
            data.get("msg"),
        )
        return None
    return data


def _extract_options(
    payload: dict[str, Any], want_instance: str
) -> list[dict[str, Any]]:
    """Pull the ENUM options of a given capability instance from a payload."""
    capabilities = (payload or {}).get("payload", {}).get("capabilities", [])
    for cap in capabilities:
        if cap.get("instance") != want_instance:
            continue
        params = cap.get("parameters", {})
        if params.get("dataType") != "ENUM":
            continue
        return params.get("options", []) or []
    return []


async def async_fetch_effects(
    session: aiohttp.ClientSession, api_key: str, sku: str, device: str
) -> dict[str, dict[str, Any]]:
    """Return a mapping of {effect_name: control_capability} for a device.

    ``control_capability`` is the dict passed straight into the v2 control call
    as ``payload.capability``. Sources combined (per user request):
      * user snapshots  (instance ``snapshot``)   - e.g. a saved "TV tracking"
      * dynamic scenes  (instance ``lightScene``)
      * DIY scenes      (instance ``diyScene``, prefixed "DIY: ")

    Any individual source that fails is skipped; the light keeps working.
    """
    effects: dict[str, dict[str, Any]] = {}

    if not api_key:
        return effects

    # --- 1) snapshots come from the device list (a GET endpoint) ----------
    devices: Any = None
    try:
        async with session.get(
            DEVICES_URL, headers=_headers(api_key), timeout=_TIMEOUT
        ) as resp:
            devices = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as ex:
        _LOGGER.debug("Govee v2 device list failed: %s", ex)
        devices = None

    if isinstance(devices, dict):
        for dev in devices.get("data", []) or devices.get("payload", []) or []:
            if dev.get("device") != device:
                continue
            for cap in dev.get("capabilities", []):
                if cap.get("instance") != "snapshot":
                    continue
                params = cap.get("parameters", {})
                if params.get("dataType") != "ENUM":
                    continue
                for opt in params.get("options", []) or []:
                    name = opt.get("name")
                    value = opt.get("value")
                    if name is None or value is None:
                        continue
                    effects[str(name)] = {
                        "type": DYNAMIC_SCENE,
                        "instance": "snapshot",
                        "value": value,
                    }

    # --- 2) dynamic light scenes -----------------------------------------
    scenes = await _post(session, SCENES_URL, api_key, _payload(sku, device))
    if scenes is not None:
        for opt in _extract_options(scenes, "lightScene"):
            name = opt.get("name")
            value = opt.get("value")
            if name is None or value is None:
                continue
            effects.setdefault(
                str(name),
                {"type": DYNAMIC_SCENE, "instance": "lightScene", "value": value},
            )

    # --- 3) DIY scenes ----------------------------------------------------
    diy = await _post(session, DIY_SCENES_URL, api_key, _payload(sku, device))
    if diy is not None:
        for opt in _extract_options(diy, "diyScene"):
            name = opt.get("name")
            value = opt.get("value")
            if name is None or value is None:
                continue
            effects[f"DIY: {name}"] = {
                "type": DYNAMIC_SCENE,
                "instance": "diyScene",
                "value": value,
            }

    _LOGGER.debug(
        "Govee v2: %d effect(s) discovered for %s (%s)", len(effects), device, sku
    )
    return effects


async def async_set_effect(
    session: aiohttp.ClientSession,
    api_key: str,
    sku: str,
    device: str,
    capability: dict[str, Any],
) -> bool:
    """Activate an effect via the v2 control endpoint. Returns True on success."""
    body = {
        "requestId": str(uuid.uuid4()),
        "payload": {"sku": sku, "device": device, "capability": capability},
    }
    result = await _post(session, CONTROL_URL, api_key, body)
    return result is not None
