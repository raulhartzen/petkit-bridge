"""
Petkit -> HTTP bridge.

Exposes a small local HTTP API that Homebridge (via generic HTTP plugins)
can query. The fragile logic of talking to the Petkit servers is delegated
to the maintained pypetkitapi library (Jezza34000).

Exposed endpoints:
  GET  /devices            -> compact list of devices (id, name, type)
  GET  /device/<id>        -> raw JSON dump of a device (to discover actual fields)
  GET  /device/<id>/state  -> compact state, best-effort, mapped for HomeKit
  POST /device/<id>/feed   -> dispense food. JSON body: {"amount": N} or {"amount1": N}/{"amount2": N}
  POST /device/<id>/clean  -> start litter box cleaning cycle
  GET  /healthz            -> ok

Authentication: the API is protected by a static token (X-Auth-Token header)
configurable via the BRIDGE_TOKEN env var. Serve on the local network ONLY.
"""

import asyncio
import logging
import os
from functools import wraps

import aiohttp
from aiohttp import web

from pypetkitapi.client import PetKitClient
from pypetkitapi.command import (
    DeviceCommand,
    FeederCommand,
    FountainCommand,
    FountainAction,
    DeviceAction,
    LBCommand,
)
from pypetkitapi.const import (
    DUAL_HOPPER_DEVICES,
    MANUAL_FEED_VALID_VALUES,
    MANUAL_FEED_DEFAULT_VALID_VALUES,
)

# Exception raised by the library when the Petkit cloud session expires.
# The module path is the one seen in tracebacks (pypetkitapi.exceptions);
# defensive import so startup doesn't break if the library reorganizes it.
try:
    from pypetkitapi.exceptions import PetkitSessionExpiredError
except Exception:  # noqa: BLE001
    class PetkitSessionExpiredError(Exception):
        """Fallback: if the import fails we won't match by type but
        by message (see _is_session_expired)."""
        pass


def _is_session_expired(exc: Exception) -> bool:
    """True if the exception indicates an expired Petkit session.
    Checks both the type and the message, because the error can also
    come from the library's internal tasks and with different types."""
    if isinstance(exc, PetkitSessionExpiredError):
        return True
    msg = str(exc).lower()
    return "session expired" in msg or "log in again" in msg

try:
    from agora.whep import WhepUpstreamManager
    _WHEP_AVAILABLE = True
except Exception as _whep_exc:  # noqa: BLE001
    _WHEP_AVAILABLE = False
    _WHEP_IMPORT_ERROR = _whep_exc

LOG = logging.getLogger("petkit-bridge")

# ----- Configuration from environment variables -----
PETKIT_USERNAME = os.environ.get("PETKIT_USERNAME", "")
PETKIT_PASSWORD = os.environ.get("PETKIT_PASSWORD", "")
PETKIT_REGION = os.environ.get("PETKIT_REGION", "IT")
PETKIT_TIMEZONE = os.environ.get("PETKIT_TIMEZONE", "Europe/Rome")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8787"))
# How often (seconds) to refresh data from the Petkit cloud.
REFRESH_INTERVAL = int(os.environ.get("PETKIT_REFRESH_INTERVAL", "60"))


def require_token(handler):
    """Decorator: rejects requests without the correct token (if configured).
    The token is accepted in the X-Auth-Token header or in the ?token= query
    (needed for go2rtc WHEP sources, which cannot send custom headers)."""

    @wraps(handler)
    async def wrapper(request: web.Request):
        if BRIDGE_TOKEN:
            provided = request.headers.get("X-Auth-Token", "")
            if not provided:
                provided = request.query.get("token", "")
            if provided != BRIDGE_TOKEN:
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return wrapper


class PetkitBridge:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._client: PetKitClient | None = None
        self._lock = asyncio.Lock()
        self.whep: "WhepUpstreamManager | None" = None
        # True when the last contact with the Petkit cloud succeeded.
        # Used by /healthz to reflect the REAL state, not just "process alive".
        self.session_ok: bool = False
        # Timestamp (monotonic) of the last successful refresh, or None.
        self._last_ok: float | None = None

    def _build_client(self):
        """(Re)builds the HTTP session and Petkit client from scratch.
        Used both at first startup and to re-login after a disconnection.
        Recreating the client replicates the same sequence as the first
        startup, without depending on the library's internal login methods
        which might change. NB: PetKitClient methods NOT verified against
        the documentation: if the library offers an explicit re-login,
        consider using it instead of rebuilding."""
        self._client = PetKitClient(
            username=PETKIT_USERNAME,
            password=PETKIT_PASSWORD,
            region=PETKIT_REGION,
            timezone=PETKIT_TIMEZONE,
            session=self._session,
        )

    async def start(self):
        """First startup. May raise if the initial login fails; the caller
        (on_startup) decides whether to treat that as fatal or not."""
        self._session = aiohttp.ClientSession()
        self._build_client()
        # First fetch: if credentials/region are wrong, it fails here.
        await self.refresh()
        if _WHEP_AVAILABLE:
            self.whep = WhepUpstreamManager(self._client)

    async def _do_relogin_locked(self):
        """Performs the ACTUAL re-login assuming self._lock is ALREADY held.
        Closes the old ClientSession, creates a new one, rebuilds the client
        and re-fetches. Does not re-acquire the lock (the caller does)."""
        old_session = self._session
        old_client = self._client
        self._client = None
        self.session_ok = False
        if old_session is not None and not old_session.closed:
            try:
                await old_session.close()
            except Exception:  # noqa: BLE001
                LOG.warning("relogin: closing old session failed (ignoring)")
        del old_client  # no documented shutdown method on the client
        self._session = aiohttp.ClientSession()
        self._build_client()
        # Fetch WITHOUT allowing another nested relogin (avoids recursion).
        await self._client.get_devices_data()
        import time as _time
        self.session_ok = True
        self._last_ok = _time.monotonic()
        if _WHEP_AVAILABLE and self.whep is not None:
            try:
                self.whep._client = self._client  # best-effort
            except Exception:  # noqa: BLE001
                LOG.warning("relogin: could not re-attach the WHEP client")

    async def relogin(self):
        """Full internal restart (acquires the lock). Replicates what
        'docker restart' does without restarting the process: closes the
        old session, creates a new one, rebuilds the client. Closing the
        session is essential because the library starts internal tasks
        (e.g. record_tasks) tied to the client, which must be abandoned
        along with the expired connection."""
        async with self._lock:
            await self._do_relogin_locked()

    async def stop(self):
        if self._session:
            await self._session.close()

    async def refresh(self, _allow_relogin: bool = True):
        """Refreshes device data from the Petkit cloud.
        Updates session_ok based on the outcome. If it hits an expired
        session, it attempts the automatic re-login ONCE and repeats the
        fetch, so HTTP requests (not just the background task) also
        self-heal. _allow_relogin=False prevents infinite recursion."""
        async with self._lock:
            if self._client is None:
                self.session_ok = False
                raise RuntimeError("Petkit client not initialized")
            try:
                await self._client.get_devices_data()
            except Exception as exc:
                self.session_ok = False
                if _allow_relogin and _is_session_expired(exc):
                    LOG.warning(
                        "refresh: session expired, attempting on-the-fly re-login"
                    )
                    # Re-login without releasing the lock (we already hold it).
                    await self._do_relogin_locked()
                    return
                raise
            import time as _time
            self.session_ok = True
            self._last_ok = _time.monotonic()

    @property
    def entities(self) -> dict:
        # client.petkit_entities is a dict {device_id: device-object}
        return self._client.petkit_entities if self._client else {}

    def _find(self, device_id: str):
        # petkit_entities keys are integers (Petkit device ids).
        ents = self.entities
        try:
            as_int = int(device_id)
            if as_int in ents:
                return ents[as_int]
        except (ValueError, TypeError):
            pass
        if device_id in ents:
            return ents[device_id]
        for key, val in ents.items():
            if str(key) == str(device_id):
                return val
        return None


bridge = PetkitBridge()


# --------------------------- Handlers HTTP ---------------------------

@require_token
async def list_devices(request: web.Request):
    try:
        await bridge.refresh()
    except Exception as exc:  # noqa: BLE001
        LOG.exception("refresh failed")
        return web.json_response({"error": str(exc)}, status=502)

    out = []
    for key, val in bridge.entities.items():
        out.append(
            {
                "id": str(key),
                "name": getattr(val, "name", None),
                "type": type(val).__name__,
            }
        )
    return web.json_response(out)


def _to_dict(dev):
    """Serializes the device object into a Python dict."""
    for attr in ("model_dump", "dict"):
        fn = getattr(dev, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                continue
    if hasattr(dev, "__dict__"):
        return dict(vars(dev))
    return {"repr": repr(dev)}


@require_token
async def device_raw(request: web.Request):
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.json_response({"error": "device not found"}, status=404)
    return web.json_response(_jsonable(_to_dict(dev)))


@require_token
async def device_state(request: web.Request):
    """Compact state mapped on the actual fields observed in dumps of these
    models: feeder d4h (Yumshare), litter t4 (Puramax 2), fountain ctw3
    (Eversweet Max). For other models use /device/<id>."""
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.json_response({"error": "device not found"}, status=404)

    raw = _to_dict(dev)
    dtype = (raw.get("device_nfo") or {}).get("device_type", "")
    st = raw.get("state") or {}
    out = {"name": raw.get("name"), "device_type": dtype}

    if dtype in ("d4", "d4h", "d4s", "d4sh", "feedermini", "d3"):
        out.update(
            {
                "category": "feeder",
                "online": st.get("pim"),               # 1 = online
                "food_level": st.get("food"),          # internal scale (e.g. 2 = ok)
                "desiccant_left_days": st.get("desiccant_left_days"),
                "battery_power": st.get("battery_power"),
                "battery_status": st.get("battery_status"),
                "bowl": st.get("bowl"),
                "door": st.get("door"),
                "feeding": st.get("feeding"),
                "error_code": st.get("error_code"),
            }
        )
    elif dtype in ("t3", "t4", "t5", "t6", "t7"):
        out.update(
            {
                "category": "litter",
                "online": st.get("pim"),
                "power": st.get("power"),
                "box_full": st.get("box_full"),
                "box_state": st.get("box_state"),
                "sand_percent": st.get("sand_percent"),
                "sand_lack": st.get("sand_lack"),
                "sand_weight": st.get("sand_weight"),
                "deodorant_left_days": st.get("deodorant_left_days"),
                "battery": st.get("battery"),
                "liquid": st.get("liquid"),
                "liquid_lack": st.get("liquid_lack"),
                "error_code": st.get("error_code"),
            }
        )
    elif dtype in ("ctw2", "ctw3", "w5"):
        elec = raw.get("electricity") or {}
        status = raw.get("status") or {}
        out.update(
            {
                "category": "fountain",
                "battery_percent": elec.get("battery_percent"),
                "low_battery": raw.get("low_battery"),
                "filter_percent": raw.get("filter_percent"),
                "filter_warning": raw.get("filter_warning"),
                "mode": raw.get("mode"),
                "lack_warning": raw.get("lack_warning"),
                "power_status": status.get("power_status"),
                "run_status": status.get("run_status"),
                "breakdown_warning": raw.get("breakdown_warning"),
            }
        )
    else:
        out["note"] = "unmapped type: use /device/<id> for the raw dump"

    return web.json_response(_jsonable(out))


@require_token
async def hk_state(request: web.Request):
    """Fountain state in the format expected by the
    homebridge-http-sensors-switches plugin. Read-only. Fields (all 0/1 or
    percentage):
      LeakDetected  : 1 if water is missing (lack_warning)
      BatteryLevel  : battery percentage (0-100)
      LowBattery    : 1 if battery is low
      StatusFault   : 1 if faulty (breakdown_warning)
      PowerOn       : 1 if the fountain is powered (power_status)
    Meant for ctw2/ctw3/w5 fountains. Returns 404 for other types."""
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.json_response({"error": "device not found"}, status=404)

    raw = _to_dict(dev)
    dtype = (raw.get("device_nfo") or {}).get("device_type", "")
    if dtype not in ("ctw2", "ctw3", "w5"):
        return web.json_response(
            {"error": f"hk-state is only supported for fountains, not '{dtype}'"},
            status=404,
        )

    elec = raw.get("electricity") or {}
    status = raw.get("status") or {}

    def _int01(v):
        try:
            return 1 if int(v) else 0
        except (TypeError, ValueError):
            return 0

    battery = elec.get("battery_percent")
    try:
        battery = max(0, min(100, int(battery)))
    except (TypeError, ValueError):
        battery = 0

    payload = {
        "LeakDetected": _int01(raw.get("lack_warning")),
        "BatteryLevel": battery,
        "LowBattery": _int01(raw.get("low_battery")),
        "StatusFault": _int01(raw.get("breakdown_warning")),
        "PowerOn": _int01(status.get("power_status")),
    }
    return web.json_response(payload)


@require_token
async def feed(request: web.Request):
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.json_response({"error": "device not found"}, status=404)

    raw = _to_dict(dev)
    dtype = (raw.get("device_nfo") or {}).get("device_type", "")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    is_dual = dtype in DUAL_HOPPER_DEVICES
    valid = MANUAL_FEED_VALID_VALUES.get(dtype, MANUAL_FEED_DEFAULT_VALID_VALUES)

    # Payload construction. Dual hopper: amount1/amount2. Single: amount.
    payload = {}
    if is_dual:
        if "amount1" in body:
            payload["amount1"] = int(body["amount1"])
        if "amount2" in body:
            payload["amount2"] = int(body["amount2"])
        if not payload:
            return web.json_response(
                {"error": "dual hopper feeder: amount1 and/or amount2 required",
                 "valid_values": valid},
                status=400,
            )
        cmd = FeederCommand.MANUAL_FEED_DUAL
    else:
        amount = int(body.get("amount", valid[0]))
        payload["amount"] = amount
        cmd = FeederCommand.MANUAL_FEED

    # Validation against the values allowed by the model.
    for k, v in payload.items():
        if v not in valid:
            return web.json_response(
                {"error": f"{k}={v} is not valid for {dtype}",
                 "valid_values": valid},
                status=400,
            )

    try:
        key = _real_key(device_id)
        await bridge._client.send_api_request(key, cmd, payload)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("feed failed")
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response({"ok": True, "device_type": dtype, "sent": payload})


@require_token
async def maint_status(request: web.Request):
    """Litter box maintenance status for the HTTP-SWITCH statusUrl.
    Plain-text response: '1' if in maintenance (work_mode == 9),
    '0' otherwise. work_state is None when the litter box is idle."""
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.Response(text="0")
    raw = _to_dict(dev)
    st = raw.get("state") or {}
    ws = st.get("work_state") or {}
    in_maint = isinstance(ws, dict) and ws.get("work_mode") == 9
    return web.Response(text="1" if in_maint else "0")


@require_token
async def scoop(request: web.Request):
    """Runs ONE full scooping cycle: START+CLEANING, wait, END+CLEANING.
    Optional JSON body: {"wait": 50} seconds to wait before the END
    (default 50, estimated cycle duration on the Puramax 2).
    The response returns immediately; START fires and END is sent after
    the wait by a background task, so the bridge stays free."""
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.json_response({"error": "device not found"}, status=404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        wait = float(body.get("wait", 50))
    except (ValueError, TypeError):
        wait = 50.0
    # Safety bounds: between 5 and 300 seconds.
    wait = max(5.0, min(wait, 300.0))

    key = _real_key(device_id)

    # START right away (awaited, so any errors come back in the response).
    try:
        await bridge._client.send_api_request(
            key, DeviceCommand.CONTROL_DEVICE,
            {DeviceAction.START: LBCommand.CLEANING},
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("scoop START failed")
        return web.json_response({"error": f"START failed: {exc}"}, status=502)

    # END after the wait, in background (does not block the response).
    async def _delayed_end():
        await asyncio.sleep(wait)
        try:
            await bridge._client.send_api_request(
                key, DeviceCommand.CONTROL_DEVICE,
                {DeviceAction.END: LBCommand.CLEANING},
            )
            LOG.info("scoop: END sent to %s after %.0fs", key, wait)
        except Exception:  # noqa: BLE001
            LOG.exception("scoop END failed")

    asyncio.create_task(_delayed_end())
    return web.json_response(
        {"ok": True, "started": True, "end_in_seconds": wait}
    )


@require_token
async def litter(request: web.Request):
    """Flexible litter box control.
    JSON body: {"action": "START", "mode": "CLEANING"}
      action: START | STOP | END | CONTINUE   (DeviceAction member)
      mode:   CLEANING | MAINTENANCE | ODOR_REMOVAL | ...  (LBCommand member)
    Useful to discover in the field which combination stops the cycle or
    exits maintenance, before wiring it into HomeKit switches."""
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.json_response({"error": "device not found"}, status=404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    action_name = body.get("action", "START")
    mode_name = body.get("mode", "CLEANING")
    if not hasattr(DeviceAction, action_name):
        return web.json_response(
            {"error": f"invalid action: {action_name}",
             "valid_values": [m for m in dir(DeviceAction) if m.isupper()]},
            status=400,
        )
    if not hasattr(LBCommand, mode_name):
        return web.json_response(
            {"error": f"invalid mode: {mode_name}",
             "valid_values": [m for m in dir(LBCommand) if m.isupper()]},
            status=400,
        )

    action_value = getattr(DeviceAction, action_name)
    lb_value = getattr(LBCommand, mode_name)
    try:
        key = _real_key(device_id)
        await bridge._client.send_api_request(
            key,
            DeviceCommand.CONTROL_DEVICE,
            {action_value: lb_value},
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("litter failed")
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response(
        {"ok": True, "action": action_name, "mode": mode_name}
    )


@require_token
async def clean(request: web.Request):
    """Controls the litter box. Optional JSON body: {"mode": "CLEANING"} or
    {"mode": "MAINTENANCE"} (default: CLEANING). The value must be a member
    of LBCommand (CLEANING, MAINTENANCE, ODOR_REMOVAL, LEVELING, ...)."""
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.json_response({"error": "device not found"}, status=404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    mode_name = body.get("mode", "CLEANING")
    if not hasattr(LBCommand, mode_name):
        valid = [m for m in dir(LBCommand) if m.isupper()]
        return web.json_response(
            {"error": f"invalid mode: {mode_name}", "valid_values": valid},
            status=400,
        )
    lb_value = getattr(LBCommand, mode_name)
    try:
        key = _real_key(device_id)
        await bridge._client.send_api_request(
            key,
            DeviceCommand.CONTROL_DEVICE,
            {DeviceAction.START: lb_value},
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("clean failed")
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response({"ok": True, "mode": mode_name})


@require_token
async def fountain(request: web.Request):
    """Controls the fountain. JSON body: {"action": "MODE_SMART"} where the
    value is one of the FountainAction members (e.g. MODE_SMART, MODE_NORMAL,
    POWER_ON, POWER_OFF, PAUSE, LIGHT_ON, LIGHT_OFF, DO_NOT_DISTURB...).
    WARNING: fountain control goes through the BLE relay and on the Pura MAX
    it can cause firmware-level lockups. Verify in the field."""
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.json_response({"error": "device not found"}, status=404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    action_name = body.get("action", "MODE_SMART")
    if not hasattr(FountainAction, action_name):
        valid = [m for m in dir(FountainAction) if not m.startswith("_")]
        return web.json_response(
            {"error": f"invalid action: {action_name}", "valid_values": valid},
            status=400,
        )
    action_value = getattr(FountainAction, action_name)
    try:
        key = _real_key(device_id)
        await bridge._client.send_api_request(
            key, FountainCommand.CONTROL_DEVICE, action_value
        )
    except Exception as exc:  # noqa: BLE001
        LOG.exception("fountain failed")
        return web.json_response({"error": str(exc)}, status=502)
    return web.json_response({"ok": True, "action": action_name})


@require_token
async def feed_all(request: web.Request):
    """Dispenses food on ALL feeders of the account at once.
    Optional JSON body: {"amount": N}. Default: minimum valid value per model.
    Dispensing starts shortly one after the other (separate requests to the
    Petkit servers), not down to the millisecond."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    requested = body.get("amount")

    results = []
    for key, dev in bridge.entities.items():
        raw = _to_dict(dev)
        dtype = (raw.get("device_nfo") or {}).get("device_type", "")
        # Single-hopper feeders only (e.g. d4h). Dual hoppers are skipped here.
        if dtype not in ("d4", "d4h", "feedermini", "d3"):
            continue
        valid = MANUAL_FEED_VALID_VALUES.get(dtype, MANUAL_FEED_DEFAULT_VALID_VALUES)
        amount = int(requested) if requested is not None else valid[0]
        if amount not in valid:
            results.append(
                {"id": str(key), "name": raw.get("name"),
                 "ok": False, "error": f"amount {amount} is not valid",
                 "valid_values": valid}
            )
            continue
        try:
            await bridge._client.send_api_request(
                key, FeederCommand.MANUAL_FEED, {"amount": amount}
            )
            results.append(
                {"id": str(key), "name": raw.get("name"),
                 "ok": True, "amount": amount}
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("feed_all: error on %s", key)
            results.append(
                {"id": str(key), "name": raw.get("name"),
                 "ok": False, "error": str(exc)}
            )

    all_ok = bool(results) and all(r["ok"] for r in results)
    status = 200 if all_ok else 207  # 207 = mixed outcome
    return web.json_response({"all_ok": all_ok, "results": results}, status=status)


@require_token
async def whep_offer(request: web.Request):
    """WHEP endpoint: go2rtc sends an SDP offer (body, content-type
    application/sdp) and receives the SDP answer. device_id in the URL."""
    if bridge.whep is None:
        return web.Response(
            status=503,
            text="WHEP not available (agora modules not loaded)",
        )
    device_id = request.match_info["device_id"]
    dev = bridge._find(device_id)
    if dev is None:
        return web.Response(status=404, text="device not found")
    offer_sdp = await request.text()
    if not offer_sdp.strip():
        return web.Response(status=400, text="empty SDP offer")
    LOG.info("WHEP offer received (%d bytes), first 60: %r",
             len(offer_sdp), offer_sdp[:60])
    try:
        key = _real_key(device_id)
        answer = await bridge.whep.create_session(int(key), offer_sdp)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("whep create_session failed")
        return web.Response(status=502, text=f"negotiation error: {exc}")
    LOG.info("WHEP answer generated (%d bytes), first 60: %r",
             len(answer or ""), (answer or "")[:60])
    return web.Response(
        status=201,
        text=answer,
        content_type="application/sdp",
        headers={"Location": f"/device/{device_id}/whep"},
    )


@require_token
async def whep_patch(request: web.Request):
    """Trickle ICE: go2rtc sends additional candidates via PATCH."""
    if bridge.whep is None:
        return web.Response(status=503)
    device_id = request.match_info["device_id"]
    fragment = await request.text()
    key = _real_key(device_id)
    ok = await bridge.whep.add_candidates(int(key), fragment)
    return web.Response(status=204 if ok else 404)


@require_token
async def whep_delete(request: web.Request):
    """Closes the WHEP session."""
    if bridge.whep is None:
        return web.Response(status=503)
    device_id = request.match_info["device_id"]
    key = _real_key(device_id)
    await bridge.whep.close_session(int(key))
    return web.Response(status=200)


async def healthz(request: web.Request):
    """Health check. By default always responds 200 (process alive).
    With ?strict=1 it responds 503 when the Petkit session is NOT valid:
    use it in the Docker healthcheck to trigger the automatic restart even
    when the process is alive but "deaf" towards the cloud."""
    import time as _time
    last_ok_age = None
    if bridge._last_ok is not None:
        last_ok_age = round(_time.monotonic() - bridge._last_ok, 1)

    payload = {
        "status": "ok" if bridge.session_ok else "degraded",
        "session_ok": bridge.session_ok,
        "last_ok_age_seconds": last_ok_age,
    }
    strict = request.query.get("strict", "") in ("1", "true", "yes")
    http_status = 503 if (strict and not bridge.session_ok) else 200
    return web.json_response(payload, status=http_status)


# --------------------------- Utilities ---------------------------

def _real_key(device_id: str):
    """Returns the real key (int) used in petkit_entities."""
    ents = bridge.entities
    try:
        as_int = int(device_id)
        if as_int in ents:
            return as_int
    except (ValueError, TypeError):
        pass
    for key in ents:
        if str(key) == str(device_id):
            return key
    return device_id


def _jsonable(obj):
    """Makes a nested object serializable (dataclass/enum/datetime)."""
    import datetime
    import enum

    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _jsonable(fn())
            except Exception:  # noqa: BLE001
                pass
    if hasattr(obj, "__dict__"):
        return _jsonable(vars(obj))
    return repr(obj)


# --------------------------- Startup ---------------------------

async def _background_refresh(app):
    """Background task: refreshes periodically to keep the session alive.

    Resilient to disconnections:
      - an isolated failure is just logged and retried on the next cycle;
      - after RELOGIN_AFTER consecutive failures it attempts a full re-login
        (client rebuild), because the session is probably dead;
      - uses an increasing backoff (capped at MAX_BACKOFF) to avoid hammering
        the Petkit cloud when it is unreachable.
    """
    # Consecutive failures after which we rebuild the client.
    relogin_after = int(os.environ.get("PETKIT_RELOGIN_AFTER", "3"))
    max_backoff = int(os.environ.get("PETKIT_MAX_BACKOFF", "300"))
    consecutive_failures = 0
    # True when the last error was an expired session: on the next cycle
    # we force the re-login with a short wait.
    session_expired = False
    try:
        while True:
            # Backoff: when failing, wait longer (up to max_backoff).
            # But on an expired session do NOT wait long: re-login is fast
            # and we want to restore service quickly.
            if session_expired:
                delay = REFRESH_INTERVAL
            elif consecutive_failures == 0:
                delay = REFRESH_INTERVAL
            else:
                delay = min(REFRESH_INTERVAL * (2 ** consecutive_failures), max_backoff)
            await asyncio.sleep(delay)

            try:
                # Expired session: IMMEDIATE re-login, don't wait for cycles.
                # Or: too many generic failures in a row -> re-login anyway.
                if session_expired or consecutive_failures >= relogin_after:
                    reason = "session expired" if session_expired else \
                        f"{consecutive_failures} consecutive refresh failures"
                    LOG.warning("%s: attempting full re-login", reason)
                    await bridge.relogin()
                else:
                    await bridge.refresh()
                if consecutive_failures or session_expired:
                    LOG.info("Petkit connection restored")
                consecutive_failures = 0
                session_expired = False
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                # If it's a session expiry, next cycle we go straight
                # to the re-login (with a short backoff, see above).
                session_expired = _is_session_expired(exc)
                LOG.exception(
                    "background refresh failed (consecutive attempts: %d, "
                    "session_expired=%s)",
                    consecutive_failures, session_expired,
                )
    except asyncio.CancelledError:
        pass


async def on_startup(app):
    if not PETKIT_USERNAME or not PETKIT_PASSWORD:
        raise RuntimeError(
            "PETKIT_USERNAME and PETKIT_PASSWORD must be set."
        )
    # By default a failed initial login does NOT kill the container:
    # the app starts anyway and the background task keeps retrying.
    # Set PETKIT_FATAL_ON_START_FAIL=1 for the old behavior (crash).
    fatal = os.environ.get("PETKIT_FATAL_ON_START_FAIL", "0") == "1"
    try:
        await bridge.start()
    except Exception as exc:  # noqa: BLE001
        LOG.error(
            "Initial Petkit login/fetch failed (%s): %s. "
            "Check credentials (DEDICATED account), PETKIT_REGION and "
            "that the account is not logged in elsewhere.",
            type(exc).__name__,
            exc,
        )
        if fatal:
            raise
        # Start anyway: the background task will re-establish the session.
        # The aiohttp session may not have been created if start() failed
        # very early: make sure there is one for later attempts.
        if bridge._session is None:
            bridge._session = aiohttp.ClientSession()
        LOG.warning(
            "Bridge started in DEGRADED state: endpoints will respond "
            "once the Petkit session is re-established."
        )
    app["refresh_task"] = asyncio.create_task(_background_refresh(app))


async def on_cleanup(app):
    task = app.get("refresh_task")
    if task:
        task.cancel()
    if bridge.whep is not None:
        await bridge.whep.close_all()
    await bridge.stop()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/devices", list_devices)
    app.router.add_get("/device/{device_id}", device_raw)
    app.router.add_get("/device/{device_id}/state", device_state)
    app.router.add_get("/device/{device_id}/hk-state", hk_state)
    app.router.add_post("/device/{device_id}/feed", feed)
    app.router.add_post("/feed-all", feed_all)
    app.router.add_post("/device/{device_id}/clean", clean)
    app.router.add_post("/device/{device_id}/litter", litter)
    app.router.add_post("/device/{device_id}/scoop", scoop)
    app.router.add_get("/device/{device_id}/maint-status", maint_status)
    app.router.add_post("/device/{device_id}/whep", whep_offer)
    app.router.add_patch("/device/{device_id}/whep", whep_patch)
    app.router.add_delete("/device/{device_id}/whep", whep_delete)
    app.router.add_post("/device/{device_id}/fountain", fountain)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    web.run_app(build_app(), host=BRIDGE_HOST, port=BRIDGE_PORT)
