from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN  # BASE_URL etc. are in const.py :contentReference[oaicite:1]{index=1}

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RAR ITP Checker from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Forward the config entry to the sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the domain service only once
    if not hass.services.has_service(DOMAIN, "check_now"):

        async def handle_check_now(call: ServiceCall) -> None:
            """Force an immediate ITP refresh for a VIN."""
            # Allow overriding VIN from service data; fall back to this entry's VIN
            vin = call.data.get("vin") or entry.data.get("vin")
            if not vin:
                _LOGGER.warning("check_now called without a VIN")
                return

            domain_data = hass.data.get(DOMAIN, {})
            entry_data = domain_data.get(vin)

            if not entry_data:
                _LOGGER.warning(
                    "check_now: no coordinator found for VIN %s (domain data: %s)",
                    vin,
                    list(domain_data.keys()),
                )
                return

            coordinator = entry_data.get("coordinator")
            if not coordinator:
                _LOGGER.warning("check_now: coordinator missing for VIN %s", vin)
                return

            _LOGGER.debug("check_now: triggering refresh for VIN %s", vin)
            await coordinator.async_request_refresh()

        hass.services.async_register(DOMAIN, "check_now", handle_check_now)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        _LOGGER.error("async_unload_entry: async_unload_platforms returned False")
        return False

    domain_data = hass.data.get(DOMAIN)
    if not domain_data:
        # Nothing to clean up
        return True

    vin = entry.data.get("vin")
    if vin:
        # Use pop(..., None) so we never raise KeyError
        removed = domain_data.pop(vin, None)
        if removed is None:
            _LOGGER.debug(
                "async_unload_entry: VIN %s not found in hass.data[%s], "
                "domain keys were: %s",
                vin,
                DOMAIN,
                list(domain_data.keys()),
            )

    # If no entries left for this domain, drop the service + domain data
    if not domain_data:
        _LOGGER.debug(
            "async_unload_entry: no more entries for %s, removing check_now service",
            DOMAIN,
        )
        hass.services.async_remove(DOMAIN, "check_now")
        hass.data.pop(DOMAIN, None)

    return True
