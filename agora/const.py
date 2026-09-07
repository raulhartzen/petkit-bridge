# Adapted from Jezza34000/homeassistant_petkit (MIT License):
# https://github.com/Jezza34000/homeassistant_petkit
# Agora reverse engineering originally by @mikey0000. See README credits.
"""Costanti minime per i moduli Agora portati."""
import logging
import os

LOGGER = logging.getLogger("petkit-bridge.agora")
AGORA_APP_ID = "244c49951296440cbc1e3b937bf5e410"

# Audio nelle sessioni camera (WHEP). Attivo di default; impostare
# PETKIT_CAMERA_AUDIO=0 per tornare al comportamento video-only.
CAMERA_AUDIO = os.environ.get("PETKIT_CAMERA_AUDIO", "1") == "1"
