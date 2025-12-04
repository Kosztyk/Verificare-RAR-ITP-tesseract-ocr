import voluptuous as vol                     # Form/schema helper for HA config flows
from homeassistant import config_entries     # Base classes for ConfigFlow & OptionsFlow
from homeassistant.const import CONF_NAME    # Standard constant for "name"
from homeassistant.core import callback      # Used by async_get_options_flow

from .const import DOMAIN                    # Our integration domain string


class RarItpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for the RAR ITP Checker integration."""

    VERSION = 1  # Version of the config flow schema

    async def async_step_user(self, user_input=None):
        """Handle the initial configuration step shown in the UI."""
        if user_input is not None:
            # user_input contains whatever the user filled in the form:
            #   - CONF_NAME
            #   - vin
            #   - tesseract_ip (optional)

            # Use VIN as unique_id → prevents adding the same VIN twice.
            await self.async_set_unique_id(user_input["vin"])
            self._abort_if_unique_id_configured()

            # Create the config entry.
            # Everything from user_input goes into .data
            return self.async_create_entry(
                title=user_input[CONF_NAME],  # What shows in the integrations list
                data=user_input,              # Store vin + tesseract_ip here
            )

        # First time we get here, show the form to the user.
        return self.async_show_form(
            step_id="user",  # Name of this step
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,   # Friendly name of the integration
                    vol.Required("vin"): str,       # VIN to monitor
                    # NEW: tesseract_ip instead of ocr_api_key
                    #
                    # You can enter either:
                    #   - just an IP/hostname → e.g. 192.168.68.144
                    #   - or a full URL     → e.g. http://192.168.68.144:8000/ocr/file?lang=eng
                    #
                    # We keep it optional so the integration still works with manual captcha
                    # or with a default URL from const.py / sensor.py.
                    vol.Optional("tesseract_ip", default=""): str,
                }
            ),
        )

    # ---------- OPTIONS FLOW HOOK ----------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """
        Tell Home Assistant which OptionsFlow to use for this config entry.

        This makes the "Configure" button open our custom options dialog
        instead of doing nothing.
        """
        return RarItpOptionsFlow(config_entry)


class RarItpOptionsFlow(config_entries.OptionsFlow):
    """Handle options (editable after setup) for RAR ITP Checker."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # Keep a reference to the config entry so we can read .data and .options
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """
        The only step of this OptionsFlow.

        It shows a simple form with tesseract_ip and saves it into config_entry.options.
        """
        if user_input is not None:
            # User pressed Submit:
            # - we store only tesseract_ip in options
            # - anything else stays in config_entry.data
            tesseract_ip = user_input.get("tesseract_ip", "").strip()

            return self.async_create_entry(
                title="",                    # Title not shown in UI for options
                data={"tesseract_ip": tesseract_ip},  # Stored in config_entry.options
            )

        # When the form is first shown, pre-fill it with the current value.
        #
        # Priority:
        #   1. config_entry.options["tesseract_ip"] (if already configured via options)
        #   2. config_entry.data["tesseract_ip"]    (if set during initial setup)
        #   3. config_entry.data["ocr_api_key"]     (old field name, for backward compat)
        #   4. empty string
        current_tesseract_ip = (
            self.config_entry.options.get("tesseract_ip")
            or self.config_entry.data.get("tesseract_ip", "")
        )

        # Show the options form
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("tesseract_ip", default=current_tesseract_ip): str,
                }
            ),
        )
