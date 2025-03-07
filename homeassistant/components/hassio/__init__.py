"""Support for Hass.io."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timedelta
import logging
import os
import re
from typing import Any, NamedTuple

import voluptuous as vol

from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.components import panel_custom
from homeassistant.components.homeassistant import async_set_stop_handler
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_MANUFACTURER,
    ATTR_NAME,
    EVENT_CORE_CONFIG_UPDATE,
    HASSIO_USER_NAME,
    Platform,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    HassJob,
    HomeAssistant,
    ServiceCall,
    async_get_hass,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.loader import bind_hass
from homeassistant.util.dt import now

from .addon_manager import AddonError, AddonInfo, AddonManager, AddonState  # noqa: F401
from .addon_panel import async_setup_addon_panel
from .auth import async_setup_auth_view
from .const import (
    ATTR_ADDON,
    ATTR_ADDONS,
    ATTR_AUTO_UPDATE,
    ATTR_CHANGELOG,
    ATTR_COMPRESSED,
    ATTR_FOLDERS,
    ATTR_HOMEASSISTANT,
    ATTR_HOMEASSISTANT_EXCLUDE_DATABASE,
    ATTR_INPUT,
    ATTR_LOCATION,
    ATTR_PASSWORD,
    ATTR_REPOSITORY,
    ATTR_SLUG,
    ATTR_STARTED,
    ATTR_STATE,
    ATTR_URL,
    ATTR_VERSION,
    CONTAINER_CHANGELOG,
    CONTAINER_INFO,
    CONTAINER_STATS,
    CORE_CONTAINER,
    DATA_KEY_ADDONS,
    DATA_KEY_CORE,
    DATA_KEY_HOST,
    DATA_KEY_OS,
    DATA_KEY_SUPERVISOR,
    DATA_KEY_SUPERVISOR_ISSUES,
    DOMAIN,
    SUPERVISOR_CONTAINER,
    SupervisorEntityModel,
)
from .discovery import HassioServiceInfo, async_setup_discovery_view  # noqa: F401
from .handler import (  # noqa: F401
    HassIO,
    HassioAPIError,
    async_create_backup,
    async_get_addon_discovery_info,
    async_get_addon_info,
    async_get_addon_store_info,
    async_get_green_settings,
    async_get_yellow_settings,
    async_install_addon,
    async_reboot_host,
    async_restart_addon,
    async_set_addon_options,
    async_set_green_settings,
    async_set_yellow_settings,
    async_start_addon,
    async_stop_addon,
    async_uninstall_addon,
    async_update_addon,
    async_update_core,
    async_update_diagnostics,
    async_update_os,
    async_update_supervisor,
)
from .http import HassIOView
from .ingress import async_setup_ingress_view
from .issues import SupervisorIssues
from .websocket_api import async_load_websocket_api

_LOGGER = logging.getLogger(__name__)


STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.UPDATE]

CONF_FRONTEND_REPO = "development_repo"

CONFIG_SCHEMA = vol.Schema(
    {vol.Optional(DOMAIN): vol.Schema({vol.Optional(CONF_FRONTEND_REPO): cv.isdir})},
    extra=vol.ALLOW_EXTRA,
)


DATA_CORE_INFO = "hassio_core_info"
DATA_CORE_STATS = "hassio_core_stats"
DATA_HOST_INFO = "hassio_host_info"
DATA_STORE = "hassio_store"
DATA_INFO = "hassio_info"
DATA_OS_INFO = "hassio_os_info"
DATA_SUPERVISOR_INFO = "hassio_supervisor_info"
DATA_SUPERVISOR_STATS = "hassio_supervisor_stats"
DATA_ADDONS_CHANGELOGS = "hassio_addons_changelogs"
DATA_ADDONS_INFO = "hassio_addons_info"
DATA_ADDONS_STATS = "hassio_addons_stats"
HASSIO_UPDATE_INTERVAL = timedelta(minutes=5)

ADDONS_COORDINATOR = "hassio_addons_coordinator"

SERVICE_ADDON_START = "addon_start"
SERVICE_ADDON_STOP = "addon_stop"
SERVICE_ADDON_RESTART = "addon_restart"
SERVICE_ADDON_UPDATE = "addon_update"
SERVICE_ADDON_STDIN = "addon_stdin"
SERVICE_HOST_SHUTDOWN = "host_shutdown"
SERVICE_HOST_REBOOT = "host_reboot"
SERVICE_BACKUP_FULL = "backup_full"
SERVICE_BACKUP_PARTIAL = "backup_partial"
SERVICE_RESTORE_FULL = "restore_full"
SERVICE_RESTORE_PARTIAL = "restore_partial"

VALID_ADDON_SLUG = vol.Match(re.compile(r"^[-_.A-Za-z0-9]+$"))


def valid_addon(value: Any) -> str:
    """Validate value is a valid addon slug."""
    value = VALID_ADDON_SLUG(value)

    hass: HomeAssistant | None = None
    with suppress(HomeAssistantError):
        hass = async_get_hass()

    if hass and (addons := get_addons_info(hass)) is not None and value not in addons:
        raise vol.Invalid("Not a valid add-on slug")
    return value


SCHEMA_NO_DATA = vol.Schema({})

SCHEMA_ADDON = vol.Schema({vol.Required(ATTR_ADDON): valid_addon})

SCHEMA_ADDON_STDIN = SCHEMA_ADDON.extend(
    {vol.Required(ATTR_INPUT): vol.Any(dict, cv.string)}
)

SCHEMA_BACKUP_FULL = vol.Schema(
    {
        vol.Optional(
            ATTR_NAME, default=lambda: now().strftime("%Y-%m-%d %H:%M:%S")
        ): cv.string,
        vol.Optional(ATTR_PASSWORD): cv.string,
        vol.Optional(ATTR_COMPRESSED): cv.boolean,
        vol.Optional(ATTR_LOCATION): vol.All(
            cv.string, lambda v: None if v == "/backup" else v
        ),
        vol.Optional(ATTR_HOMEASSISTANT_EXCLUDE_DATABASE): cv.boolean,
    }
)

SCHEMA_BACKUP_PARTIAL = SCHEMA_BACKUP_FULL.extend(
    {
        vol.Optional(ATTR_HOMEASSISTANT): cv.boolean,
        vol.Optional(ATTR_FOLDERS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_ADDONS): vol.All(cv.ensure_list, [cv.slug]),
    }
)

SCHEMA_RESTORE_FULL = vol.Schema(
    {
        vol.Required(ATTR_SLUG): cv.slug,
        vol.Optional(ATTR_PASSWORD): cv.string,
    }
)

SCHEMA_RESTORE_PARTIAL = SCHEMA_RESTORE_FULL.extend(
    {
        vol.Optional(ATTR_HOMEASSISTANT): cv.boolean,
        vol.Optional(ATTR_FOLDERS): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_ADDONS): vol.All(cv.ensure_list, [cv.slug]),
    }
)


class APIEndpointSettings(NamedTuple):
    """Settings for API endpoint."""

    command: str
    schema: vol.Schema
    timeout: int | None = 60
    pass_data: bool = False


MAP_SERVICE_API = {
    SERVICE_ADDON_START: APIEndpointSettings("/addons/{addon}/start", SCHEMA_ADDON),
    SERVICE_ADDON_STOP: APIEndpointSettings("/addons/{addon}/stop", SCHEMA_ADDON),
    SERVICE_ADDON_RESTART: APIEndpointSettings("/addons/{addon}/restart", SCHEMA_ADDON),
    SERVICE_ADDON_UPDATE: APIEndpointSettings("/addons/{addon}/update", SCHEMA_ADDON),
    SERVICE_ADDON_STDIN: APIEndpointSettings(
        "/addons/{addon}/stdin", SCHEMA_ADDON_STDIN
    ),
    SERVICE_HOST_SHUTDOWN: APIEndpointSettings("/host/shutdown", SCHEMA_NO_DATA),
    SERVICE_HOST_REBOOT: APIEndpointSettings("/host/reboot", SCHEMA_NO_DATA),
    SERVICE_BACKUP_FULL: APIEndpointSettings(
        "/backups/new/full",
        SCHEMA_BACKUP_FULL,
        None,
        True,
    ),
    SERVICE_BACKUP_PARTIAL: APIEndpointSettings(
        "/backups/new/partial",
        SCHEMA_BACKUP_PARTIAL,
        None,
        True,
    ),
    SERVICE_RESTORE_FULL: APIEndpointSettings(
        "/backups/{slug}/restore/full",
        SCHEMA_RESTORE_FULL,
        None,
        True,
    ),
    SERVICE_RESTORE_PARTIAL: APIEndpointSettings(
        "/backups/{slug}/restore/partial",
        SCHEMA_RESTORE_PARTIAL,
        None,
        True,
    ),
}

HARDWARE_INTEGRATIONS = {
    "green": "homeassistant_green",
    "odroid-c2": "hardkernel",
    "odroid-c4": "hardkernel",
    "odroid-m1": "hardkernel",
    "odroid-n2": "hardkernel",
    "odroid-xu4": "hardkernel",
    "rpi2": "raspberry_pi",
    "rpi3": "raspberry_pi",
    "rpi3-64": "raspberry_pi",
    "rpi4": "raspberry_pi",
    "rpi4-64": "raspberry_pi",
    "yellow": "homeassistant_yellow",
}


def hostname_from_addon_slug(addon_slug: str) -> str:
    """Return hostname of add-on."""
    return addon_slug.replace("_", "-")


@callback
@bind_hass
def get_info(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return generic information from Supervisor.

    Async friendly.
    """
    return hass.data.get(DATA_INFO)


@callback
@bind_hass
def get_host_info(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return generic host information.

    Async friendly.
    """
    return hass.data.get(DATA_HOST_INFO)


@callback
@bind_hass
def get_store(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return store information.

    Async friendly.
    """
    return hass.data.get(DATA_STORE)


@callback
@bind_hass
def get_supervisor_info(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return Supervisor information.

    Async friendly.
    """
    return hass.data.get(DATA_SUPERVISOR_INFO)


@callback
@bind_hass
def get_addons_info(hass: HomeAssistant) -> dict[str, dict[str, Any]] | None:
    """Return Addons info.

    Async friendly.
    """
    return hass.data.get(DATA_ADDONS_INFO)


@callback
@bind_hass
def get_addons_stats(hass):
    """Return Addons stats.

    Async friendly.
    """
    return hass.data.get(DATA_ADDONS_STATS)


@callback
@bind_hass
def get_core_stats(hass):
    """Return core stats.

    Async friendly.
    """
    return hass.data.get(DATA_CORE_STATS)


@callback
@bind_hass
def get_supervisor_stats(hass):
    """Return supervisor stats.

    Async friendly.
    """
    return hass.data.get(DATA_SUPERVISOR_STATS)


@callback
@bind_hass
def get_addons_changelogs(hass):
    """Return Addons changelogs.

    Async friendly.
    """
    return hass.data.get(DATA_ADDONS_CHANGELOGS)


@callback
@bind_hass
def get_os_info(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return OS information.

    Async friendly.
    """
    return hass.data.get(DATA_OS_INFO)


@callback
@bind_hass
def get_core_info(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return Home Assistant Core information from Supervisor.

    Async friendly.
    """
    return hass.data.get(DATA_CORE_INFO)


@callback
@bind_hass
def get_issues_info(hass: HomeAssistant) -> SupervisorIssues | None:
    """Return Supervisor issues info.

    Async friendly.
    """
    return hass.data.get(DATA_KEY_SUPERVISOR_ISSUES)


@callback
@bind_hass
def is_hassio(hass: HomeAssistant) -> bool:
    """Return true if Hass.io is loaded.

    Async friendly.
    """
    return DOMAIN in hass.config.components


@callback
def get_supervisor_ip() -> str | None:
    """Return the supervisor ip address."""
    if "SUPERVISOR" not in os.environ:
        return None
    return os.environ["SUPERVISOR"].partition(":")[0]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:  # noqa: C901
    """Set up the Hass.io component."""
    # Check local setup
    for env in ("SUPERVISOR", "SUPERVISOR_TOKEN"):
        if os.environ.get(env):
            continue
        _LOGGER.error("Missing %s environment variable", env)
        if config_entries := hass.config_entries.async_entries(DOMAIN):
            hass.async_create_task(
                hass.config_entries.async_remove(config_entries[0].entry_id)
            )
        return False

    async_load_websocket_api(hass)

    host = os.environ["SUPERVISOR"]
    websession = async_get_clientsession(hass)
    hass.data[DOMAIN] = hassio = HassIO(hass.loop, websession, host)

    if not await hassio.is_connected():
        _LOGGER.warning("Not connected with the supervisor / system too busy!")

    store = Store[dict[str, str]](hass, STORAGE_VERSION, STORAGE_KEY)
    if (data := await store.async_load()) is None:
        data = {}

    refresh_token = None
    if "hassio_user" in data:
        user = await hass.auth.async_get_user(data["hassio_user"])
        if user and user.refresh_tokens:
            refresh_token = list(user.refresh_tokens.values())[0]

            # Migrate old Hass.io users to be admin.
            if not user.is_admin:
                await hass.auth.async_update_user(user, group_ids=[GROUP_ID_ADMIN])

            # Migrate old name
            if user.name == "Hass.io":
                await hass.auth.async_update_user(user, name=HASSIO_USER_NAME)

    if refresh_token is None:
        user = await hass.auth.async_create_system_user(
            HASSIO_USER_NAME, group_ids=[GROUP_ID_ADMIN]
        )
        refresh_token = await hass.auth.async_create_refresh_token(user)
        data["hassio_user"] = user.id
        await store.async_save(data)

    # This overrides the normal API call that would be forwarded
    development_repo = config.get(DOMAIN, {}).get(CONF_FRONTEND_REPO)
    if development_repo is not None:
        hass.http.register_static_path(
            "/api/hassio/app", os.path.join(development_repo, "hassio/build"), False
        )

    hass.http.register_view(HassIOView(host, websession))

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="hassio",
        webcomponent_name="hassio-main",
        js_url="/api/hassio/app/entrypoint.js",
        embed_iframe=True,
        require_admin=True,
    )

    await hassio.update_hass_api(config.get("http", {}), refresh_token)

    last_timezone = None

    async def push_config(_):
        """Push core config to Hass.io."""
        nonlocal last_timezone

        new_timezone = str(hass.config.time_zone)

        if new_timezone == last_timezone:
            return

        last_timezone = new_timezone
        await hassio.update_hass_timezone(new_timezone)

    hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, push_config)

    await push_config(None)

    async def async_service_handler(service: ServiceCall) -> None:
        """Handle service calls for Hass.io."""
        api_endpoint = MAP_SERVICE_API[service.service]

        data = service.data.copy()
        addon = data.pop(ATTR_ADDON, None)
        slug = data.pop(ATTR_SLUG, None)
        payload = None

        # Pass data to Hass.io API
        if service.service == SERVICE_ADDON_STDIN:
            payload = data[ATTR_INPUT]
        elif api_endpoint.pass_data:
            payload = data

        # Call API
        # The exceptions are logged properly in hassio.send_command
        with suppress(HassioAPIError):
            await hassio.send_command(
                api_endpoint.command.format(addon=addon, slug=slug),
                payload=payload,
                timeout=api_endpoint.timeout,
            )

    for service, settings in MAP_SERVICE_API.items():
        hass.services.async_register(
            DOMAIN, service, async_service_handler, schema=settings.schema
        )

    async def update_info_data(_: datetime | None = None) -> None:
        """Update last available supervisor information."""

        try:
            (
                hass.data[DATA_INFO],
                hass.data[DATA_HOST_INFO],
                hass.data[DATA_STORE],
                hass.data[DATA_CORE_INFO],
                hass.data[DATA_SUPERVISOR_INFO],
                hass.data[DATA_OS_INFO],
            ) = await asyncio.gather(
                hassio.get_info(),
                hassio.get_host_info(),
                hassio.get_store(),
                hassio.get_core_info(),
                hassio.get_supervisor_info(),
                hassio.get_os_info(),
            )

        except HassioAPIError as err:
            _LOGGER.warning("Can't read Supervisor data: %s", err)

        async_call_later(
            hass,
            HASSIO_UPDATE_INTERVAL,
            HassJob(update_info_data, cancel_on_shutdown=True),
        )

    # Fetch data
    await update_info_data()

    async def _async_stop(hass: HomeAssistant, restart: bool) -> None:
        """Stop or restart home assistant."""
        if restart:
            await hassio.restart_homeassistant()
        else:
            await hassio.stop_homeassistant()

    # Set a custom handler for the homeassistant.restart and homeassistant.stop services
    async_set_stop_handler(hass, _async_stop)

    # Init discovery Hass.io feature
    async_setup_discovery_view(hass, hassio)

    # Init auth Hass.io feature
    assert user is not None
    async_setup_auth_view(hass, user)

    # Init ingress Hass.io feature
    async_setup_ingress_view(hass, host)

    # Init add-on ingress panels
    await async_setup_addon_panel(hass, hassio)

    # Setup hardware integration for the detected board type
    async def _async_setup_hardware_integration(_: datetime | None = None) -> None:
        """Set up hardaware integration for the detected board type."""
        if (os_info := get_os_info(hass)) is None:
            # os info not yet fetched from supervisor, retry later
            async_call_later(
                hass,
                HASSIO_UPDATE_INTERVAL,
                async_setup_hardware_integration_job,
            )
            return
        if (board := os_info.get("board")) is None:
            return
        if (hw_integration := HARDWARE_INTEGRATIONS.get(board)) is None:
            return
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                hw_integration, context={"source": "system"}
            )
        )

    async_setup_hardware_integration_job = HassJob(
        _async_setup_hardware_integration, cancel_on_shutdown=True
    )

    await _async_setup_hardware_integration()

    hass.async_create_task(
        hass.config_entries.flow.async_init(DOMAIN, context={"source": "system"})
    )

    # Start listening for problems with supervisor and making issues
    hass.data[DATA_KEY_SUPERVISOR_ISSUES] = issues = SupervisorIssues(hass, hassio)
    await issues.setup()

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""
    dev_reg = dr.async_get(hass)
    coordinator = HassioDataUpdateCoordinator(hass, entry, dev_reg)
    await coordinator.async_config_entry_first_refresh()
    hass.data[ADDONS_COORDINATOR] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Pop add-on data
    hass.data.pop(ADDONS_COORDINATOR, None)

    return unload_ok


@callback
def async_register_addons_in_dev_reg(
    entry_id: str, dev_reg: dr.DeviceRegistry, addons: list[dict[str, Any]]
) -> None:
    """Register addons in the device registry."""
    for addon in addons:
        params = DeviceInfo(
            identifiers={(DOMAIN, addon[ATTR_SLUG])},
            model=SupervisorEntityModel.ADDON,
            sw_version=addon[ATTR_VERSION],
            name=addon[ATTR_NAME],
            entry_type=dr.DeviceEntryType.SERVICE,
            configuration_url=f"homeassistant://hassio/addon/{addon[ATTR_SLUG]}",
        )
        if manufacturer := addon.get(ATTR_REPOSITORY) or addon.get(ATTR_URL):
            params[ATTR_MANUFACTURER] = manufacturer
        dev_reg.async_get_or_create(config_entry_id=entry_id, **params)


@callback
def async_register_os_in_dev_reg(
    entry_id: str, dev_reg: dr.DeviceRegistry, os_dict: dict[str, Any]
) -> None:
    """Register OS in the device registry."""
    params = DeviceInfo(
        identifiers={(DOMAIN, "OS")},
        manufacturer="Home Assistant",
        model=SupervisorEntityModel.OS,
        sw_version=os_dict[ATTR_VERSION],
        name="Home Assistant Operating System",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    dev_reg.async_get_or_create(config_entry_id=entry_id, **params)


@callback
def async_register_host_in_dev_reg(
    entry_id: str,
    dev_reg: dr.DeviceRegistry,
) -> None:
    """Register host in the device registry."""
    params = DeviceInfo(
        identifiers={(DOMAIN, "host")},
        manufacturer="Home Assistant",
        model=SupervisorEntityModel.HOST,
        name="Home Assistant Host",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    dev_reg.async_get_or_create(config_entry_id=entry_id, **params)


@callback
def async_register_core_in_dev_reg(
    entry_id: str,
    dev_reg: dr.DeviceRegistry,
    core_dict: dict[str, Any],
) -> None:
    """Register OS in the device registry."""
    params = DeviceInfo(
        identifiers={(DOMAIN, "core")},
        manufacturer="Home Assistant",
        model=SupervisorEntityModel.CORE,
        sw_version=core_dict[ATTR_VERSION],
        name="Home Assistant Core",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    dev_reg.async_get_or_create(config_entry_id=entry_id, **params)


@callback
def async_register_supervisor_in_dev_reg(
    entry_id: str,
    dev_reg: dr.DeviceRegistry,
    supervisor_dict: dict[str, Any],
) -> None:
    """Register OS in the device registry."""
    params = DeviceInfo(
        identifiers={(DOMAIN, "supervisor")},
        manufacturer="Home Assistant",
        model=SupervisorEntityModel.SUPERVIOSR,
        sw_version=supervisor_dict[ATTR_VERSION],
        name="Home Assistant Supervisor",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    dev_reg.async_get_or_create(config_entry_id=entry_id, **params)


@callback
def async_remove_addons_from_dev_reg(
    dev_reg: dr.DeviceRegistry, addons: set[str]
) -> None:
    """Remove addons from the device registry."""
    for addon_slug in addons:
        if dev := dev_reg.async_get_device(identifiers={(DOMAIN, addon_slug)}):
            dev_reg.async_remove_device(dev.id)


class HassioDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to retrieve Hass.io status."""

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, dev_reg: dr.DeviceRegistry
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=HASSIO_UPDATE_INTERVAL,
        )
        self.hassio: HassIO = hass.data[DOMAIN]
        self.data = {}
        self.entry_id = config_entry.entry_id
        self.dev_reg = dev_reg
        self.is_hass_os = (get_info(self.hass) or {}).get("hassos") is not None
        self._container_updates: defaultdict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data via library."""
        is_first_update = not self.data

        try:
            await self.force_data_refresh(is_first_update)
        except HassioAPIError as err:
            raise UpdateFailed(f"Error on Supervisor API: {err}") from err

        new_data: dict[str, Any] = {}
        supervisor_info = get_supervisor_info(self.hass) or {}
        addons_info = get_addons_info(self.hass) or {}
        addons_stats = get_addons_stats(self.hass)
        addons_changelogs = get_addons_changelogs(self.hass)
        store_data = get_store(self.hass) or {}

        repositories = {
            repo[ATTR_SLUG]: repo[ATTR_NAME]
            for repo in store_data.get("repositories", [])
        }

        new_data[DATA_KEY_ADDONS] = {
            addon[ATTR_SLUG]: {
                **addon,
                **((addons_stats or {}).get(addon[ATTR_SLUG]) or {}),
                ATTR_AUTO_UPDATE: (addons_info.get(addon[ATTR_SLUG]) or {}).get(
                    ATTR_AUTO_UPDATE, False
                ),
                ATTR_CHANGELOG: (addons_changelogs or {}).get(addon[ATTR_SLUG]),
                ATTR_REPOSITORY: repositories.get(
                    addon.get(ATTR_REPOSITORY), addon.get(ATTR_REPOSITORY, "")
                ),
            }
            for addon in supervisor_info.get("addons", [])
        }
        if self.is_hass_os:
            new_data[DATA_KEY_OS] = get_os_info(self.hass)

        new_data[DATA_KEY_CORE] = {
            **(get_core_info(self.hass) or {}),
            **get_core_stats(self.hass),
        }
        new_data[DATA_KEY_SUPERVISOR] = {
            **supervisor_info,
            **get_supervisor_stats(self.hass),
        }
        new_data[DATA_KEY_HOST] = get_host_info(self.hass) or {}

        # If this is the initial refresh, register all addons and return the dict
        if is_first_update:
            async_register_addons_in_dev_reg(
                self.entry_id, self.dev_reg, new_data[DATA_KEY_ADDONS].values()
            )
            async_register_core_in_dev_reg(
                self.entry_id, self.dev_reg, new_data[DATA_KEY_CORE]
            )
            async_register_supervisor_in_dev_reg(
                self.entry_id, self.dev_reg, new_data[DATA_KEY_SUPERVISOR]
            )
            async_register_host_in_dev_reg(self.entry_id, self.dev_reg)
            if self.is_hass_os:
                async_register_os_in_dev_reg(
                    self.entry_id, self.dev_reg, new_data[DATA_KEY_OS]
                )

        # Remove add-ons that are no longer installed from device registry
        supervisor_addon_devices = {
            list(device.identifiers)[0][1]
            for device in self.dev_reg.devices.values()
            if self.entry_id in device.config_entries
            and device.model == SupervisorEntityModel.ADDON
        }
        if stale_addons := supervisor_addon_devices - set(new_data[DATA_KEY_ADDONS]):
            async_remove_addons_from_dev_reg(self.dev_reg, stale_addons)

        if not self.is_hass_os and (
            dev := self.dev_reg.async_get_device(identifiers={(DOMAIN, "OS")})
        ):
            # Remove the OS device if it exists and the installation is not hassos
            self.dev_reg.async_remove_device(dev.id)

        # If there are new add-ons, we should reload the config entry so we can
        # create new devices and entities. We can return an empty dict because
        # coordinator will be recreated.
        if self.data and set(new_data[DATA_KEY_ADDONS]) - set(
            self.data[DATA_KEY_ADDONS]
        ):
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.entry_id)
            )
            return {}

        return new_data

    async def force_info_update_supervisor(self) -> None:
        """Force update of the supervisor info."""
        self.hass.data[DATA_SUPERVISOR_INFO] = await self.hassio.get_supervisor_info()
        await self.async_refresh()

    async def force_data_refresh(self, first_update: bool) -> None:
        """Force update of the addon info."""
        container_updates = self._container_updates

        data = self.hass.data
        hassio = self.hassio
        updates = {
            DATA_INFO: hassio.get_info(),
            DATA_CORE_INFO: hassio.get_core_info(),
            DATA_SUPERVISOR_INFO: hassio.get_supervisor_info(),
            DATA_OS_INFO: hassio.get_os_info(),
        }
        if first_update or CONTAINER_STATS in container_updates[CORE_CONTAINER]:
            updates[DATA_CORE_STATS] = hassio.get_core_stats()
        if first_update or CONTAINER_STATS in container_updates[SUPERVISOR_CONTAINER]:
            updates[DATA_SUPERVISOR_STATS] = hassio.get_supervisor_stats()

        results = await asyncio.gather(*updates.values())
        for key, result in zip(updates, results):
            data[key] = result

        _addon_data = data[DATA_SUPERVISOR_INFO].get("addons", [])
        all_addons: list[str] = []
        started_addons: list[str] = []
        for addon in _addon_data:
            slug = addon[ATTR_SLUG]
            all_addons.append(slug)
            if addon[ATTR_STATE] == ATTR_STARTED:
                started_addons.append(slug)
        #
        # Update add-on info if its the first update or
        # there is at least one entity that needs the data.
        #
        # When entities are added they call async_enable_container_updates
        # to enable updates for the endpoints they need via
        # async_added_to_hass. This ensures that we only update
        # the data for the endpoints that are needed to avoid unnecessary
        # API calls since otherwise we would fetch stats for all containers
        # and throw them away.
        #
        for data_key, update_func, enabled_key, wanted_addons in (
            (
                DATA_ADDONS_STATS,
                self._update_addon_stats,
                CONTAINER_STATS,
                started_addons,
            ),
            (
                DATA_ADDONS_CHANGELOGS,
                self._update_addon_changelog,
                CONTAINER_CHANGELOG,
                all_addons,
            ),
            (DATA_ADDONS_INFO, self._update_addon_info, CONTAINER_INFO, all_addons),
        ):
            container_data: dict[str, Any] = data.setdefault(data_key, {})
            container_data.update(
                dict(
                    await asyncio.gather(
                        *[
                            update_func(slug)
                            for slug in wanted_addons
                            if first_update or enabled_key in container_updates[slug]
                        ]
                    )
                )
            )

    async def _update_addon_stats(self, slug: str) -> tuple[str, dict[str, Any] | None]:
        """Update single addon stats."""
        try:
            stats = await self.hassio.get_addon_stats(slug)
            return (slug, stats)
        except HassioAPIError as err:
            _LOGGER.warning("Could not fetch stats for %s: %s", slug, err)
        return (slug, None)

    async def _update_addon_changelog(self, slug: str) -> tuple[str, str | None]:
        """Return the changelog for an add-on."""
        try:
            changelog = await self.hassio.get_addon_changelog(slug)
            return (slug, changelog)
        except HassioAPIError as err:
            _LOGGER.warning("Could not fetch changelog for %s: %s", slug, err)
        return (slug, None)

    async def _update_addon_info(self, slug: str) -> tuple[str, dict[str, Any] | None]:
        """Return the info for an add-on."""
        try:
            info = await self.hassio.get_addon_info(slug)
            return (slug, info)
        except HassioAPIError as err:
            _LOGGER.warning("Could not fetch info for %s: %s", slug, err)
        return (slug, None)

    @callback
    def async_enable_container_updates(
        self, slug: str, entity_id: str, types: set[str]
    ) -> CALLBACK_TYPE:
        """Enable updates for an add-on."""
        enabled_updates = self._container_updates[slug]
        for key in types:
            enabled_updates[key].add(entity_id)

        @callback
        def _remove():
            for key in types:
                enabled_updates[key].remove(entity_id)

        return _remove

    async def _async_refresh(
        self,
        log_failures: bool = True,
        raise_on_auth_failed: bool = False,
        scheduled: bool = False,
        raise_on_entry_error: bool = False,
    ) -> None:
        """Refresh data."""
        if not scheduled:
            # Force refreshing updates for non-scheduled updates
            try:
                await self.hassio.refresh_updates()
            except HassioAPIError as err:
                _LOGGER.warning("Error on Supervisor API: %s", err)
        await super()._async_refresh(
            log_failures, raise_on_auth_failed, scheduled, raise_on_entry_error
        )
