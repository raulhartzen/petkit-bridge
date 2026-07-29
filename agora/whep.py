"""Sessione WHEP standalone: ponte tra go2rtc (ingest WebRTC) e il cloud Agora
di Petkit. Riadattato da whep_proxy.py + camera.py di homeassistant_petkit
(Jezza34000, MIT), rimuovendo ogni dipendenza da Home Assistant.

Flusso:
  go2rtc invia un offer SDP via HTTP (WHEP) all'endpoint del bridge.
  Questo modulo prende i token live (get_live_feed), interroga gli edge Agora
  (choose_server), negozia via WebSocket e ritorna l'answer SDP a go2rtc.
  Il video poi fluisce direttamente Agora -> go2rtc via WebRTC/RTP.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field

from webrtc_models import RTCIceCandidateInit, RTCIceServer
from sdp_transform import parse as sdp_parse

from .const import LOGGER, AGORA_APP_ID
from .agora_api import SERVICE_IDS, AgoraAPIClient, AgoraResponse
from .agora_rtm import AgoraRTMSignaling
from .agora_websocket import AgoraWebSocketHandler


@dataclass
class UpstreamSession:
    session_id: str
    device_id: str
    handler: AgoraWebSocketHandler
    rtm: AgoraRTMSignaling
    refresh_task: asyncio.Task | None = None


def _filter_candidates(candidates, agora_response: AgoraResponse):
    """Preferisce relay/srflx, scarta host. (da camera.py)"""
    valid_ips = {addr.ip for addr in (agora_response.get_turn_addresses() or [])}

    def is_valid(cand: str) -> bool:
        if "typ srflx" in cand or "typ prflx" in cand:
            return True
        if "typ relay" in cand:
            return not valid_ips or any(ip in cand for ip in valid_ips)
        return False

    filtered = [c for c in candidates if is_valid(c.candidate or "")]
    return filtered or candidates


def _parse_trickle_candidates(sdp_fragment: str):
    """Estrae ICE candidates trickled da un frammento SDP WHEP. (da whep_proxy.py)"""
    try:
        parsed = sdp_parse(sdp_fragment)
    except Exception:  # noqa: BLE001
        return []
    candidates = []
    for media in parsed.get("media", []) or []:
        mid = media.get("mid")
        mline_index = media.get("mLineIndex")
        for candidate in media.get("candidates", []) or []:
            line = (
                f"candidate:{candidate.get('foundation','0')} "
                f"{candidate.get('component',1)} "
                f"{candidate.get('transport','udp')} "
                f"{candidate.get('priority',0)} "
                f"{candidate.get('ip','')} {candidate.get('port',0)} "
                f"typ {candidate.get('type','host')}"
            )
            candidates.append(
                RTCIceCandidateInit(
                    candidate=line,
                    sdp_mid=str(mid) if mid is not None else None,
                    sdp_m_line_index=(
                        int(mline_index) if isinstance(mline_index, int) else None
                    ),
                )
            )
    return candidates


class WhepUpstreamManager:
    """Gestisce una sessione Agora per dispositivo per l'ingest go2rtc."""

    def __init__(self, client):
        # client = istanza PetKitClient di pypetkitapi (gia' loggata)
        self._client = client
        self._lock = asyncio.Lock()
        self._sessions: dict[str, UpstreamSession] = {}

    async def _live_feed(self, device_id: int):
        lf = await self._client.get_live_feed(device_id)
        if lf is None or not lf.channel_id or not lf.rtc_token:
            return None
        return lf

    async def create_session(self, device_id: int, offer_sdp: str) -> str | None:
        """Negozia con Agora e ritorna l'answer SDP per go2rtc."""
        dev = str(device_id)
        await self.close_session(device_id)

        live_feed = await self._live_feed(device_id)
        if live_feed is None:
            raise RuntimeError("Live feed non disponibile o token mancanti")

        async with AgoraAPIClient() as agora_client:
            agora_response = await agora_client.choose_server(
                app_id=AGORA_APP_ID,
                token=live_feed.rtc_token,
                channel_name=live_feed.channel_id,
                user_id=live_feed.uid,
                service_flags=[
                    SERVICE_IDS["CHOOSE_SERVER"],
                    SERVICE_IDS["CLOUD_PROXY_FALLBACK"],
                ],
            )
        if agora_response is None:
            raise RuntimeError("Edge server Agora non recuperati")

        rtm = AgoraRTMSignaling(AGORA_APP_ID)

        async def refresh_rtc_token():
            lf = await self._live_feed(device_id)
            if lf is None or not lf.rtc_token:
                return None
            await rtm.update_tokens(lf)
            return lf.rtc_token

        def _on_lost():
            asyncio.create_task(self.close_session(device_id))

        handler = AgoraWebSocketHandler(
            rtc_token_provider=refresh_rtc_token,
            prefer_instant_video=True,
            subscribe_retry_delay=1.0,
            subscribe_retry_attempts=3,
            declare_remote_video_ssrc=True,
            disable_audio_answer=True,
            on_connection_lost=_on_lost,
        )

        for line in offer_sdp.splitlines():
            stripped = line.strip()
            if stripped.startswith("a=candidate:"):
                handler.add_ice_candidate(
                    RTCIceCandidateInit(candidate=stripped.removeprefix("a="))
                )
        handler.candidates = _filter_candidates(handler.candidates, agora_response)

        rtm_started = await rtm.start_live(live_feed)
        if not rtm_started:
            LOGGER.warning("RTM start_live non attivo per %s", dev)

        session_id = secrets.token_hex(16)
        try:
            answer_sdp = await handler.connect_and_join(
                live_feed=live_feed,
                offer_sdp=offer_sdp,
                session_id=session_id,
                app_id=AGORA_APP_ID,
                agora_response=agora_response,
            )
        except Exception:
            await asyncio.gather(
                handler.disconnect(),
                rtm.stop_live(send_stop=True),
                return_exceptions=True,
            )
            raise

        if not answer_sdp:
            await asyncio.gather(
                handler.disconnect(),
                rtm.stop_live(send_stop=True),
                return_exceptions=True,
            )
            raise RuntimeError("Negoziazione Agora senza answer SDP")

        session = UpstreamSession(
            session_id=session_id, device_id=dev, handler=handler, rtm=rtm
        )
        session.refresh_task = asyncio.create_task(self._refresh_loop(session))
        async with self._lock:
            self._sessions[dev] = session
        return answer_sdp

    async def add_candidates(self, device_id: int, sdp_fragment: str) -> bool:
        dev = str(device_id)
        session = self._sessions.get(dev)
        if session is None:
            return False
        for cand in _parse_trickle_candidates(sdp_fragment):
            session.handler.add_ice_candidate(cand)
        return True

    async def close_session(self, device_id: int) -> bool:
        dev = str(device_id)
        async with self._lock:
            session = self._sessions.pop(dev, None)
        if session is None:
            return False
        if session.refresh_task:
            session.refresh_task.cancel()
        await asyncio.gather(
            session.handler.disconnect(),
            session.rtm.stop_live(send_stop=True),
            return_exceptions=True,
        )
        return True

    async def close_all(self):
        for dev in list(self._sessions.keys()):
            await self.close_session(int(dev))

    async def _refresh_loop(self, session: UpstreamSession):
        try:
            while True:
                await asyncio.sleep(20 * 60)
                lf = await self._live_feed(int(session.device_id))
                if lf is not None:
                    await session.rtm.update_tokens(lf)
        except asyncio.CancelledError:
            pass
