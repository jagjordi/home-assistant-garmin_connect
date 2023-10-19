"""The Garmin Connect integration."""
from datetime import date
from datetime import datetime
from datetime import timedelta
import logging
import asyncio
from collections.abc import Awaitable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.weight_scale_message import WeightScaleMessage
from fit_tool.profile.profile_type import Manufacturer, FileType

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, IntegrationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed


from .const import (
    DATA_COORDINATOR,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    GEAR,
    SERVICE_SETTING,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

def create_body_composition_fit_file(timestamp, weight=None, percent_fat=None, 
                                     percent_hydration=None, visceral_fat_mass=None, 
                                     bone_mass=None, muscle_mass=None):
                                         
    file_id_message = FileIdMessage()
    file_id_message.type = FileType.WEIGHT
    file_id_message.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id_message.product = 0
    file_id_message.time_created = round(timestamp.timestamp() * 1000)
    file_id_message.serial_number = 0x12345678

    weightmsg = WeightScaleMessage()
    weightmsg.weight = weight
    weightmsg.timestamp = round(timestamp.timestamp() * 1000)
    weightmsg.percent_fat = percent_fat
    weightmsg.percent_hydration = percent_hydration
    weightmsg.visceral_fat_mass = visceral_fat_mass
    weightmsg.bone_mass = bone_mass
    weightmsg.muscle_mass = muscle_mass
    weightmsg = [weightmsg]

    builder = FitFileBuilder(auto_define=True, min_string_size=50)
    builder.add(file_id_message)
    builder.add_all(weightmsg)

    fit_file = builder.build()

    fn = '/config/custom_components/garmin_connect/tmp.fit'
    if os.path.isfile(fn):
        os.remove(fn)
    fit_file.to_file(fn)
    return fn

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Garmin Connect from a config entry."""

    coordinator = GarminConnectDataUpdateCoordinator(hass, entry=entry)

    if not await coordinator.async_login():
        return False

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {DATA_COORDINATOR: coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    def handle_update_weight(call):
        """Handle the service call."""
        weight = call.data.get("weight", 50)
        percent_fat = call.data.get("percent_fat", None)
        percent_hydration = call.data.get("percent_hydration", None)
        visceral_fat_mass = call.data.get("visceral_fat_mass", None)
        bone_mass = call.data.get("bone_mass", None)
        muscle_mass = call.data.get("muscle_mass", None)

        create_body_composition_fit_file(datetime.now(), weight, percent_fat, percent_hydration, visceral_fat_mass, bone_mass, muscle_mass)
        coordinator._api.upload_activity("/config/custom_components/garmin_connect/tmp.fit")


    hass.services.async_register(DOMAIN, "update_weight", handle_update_weight)
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class GarminConnectDataUpdateCoordinator(DataUpdateCoordinator):
    """Garmin Connect Data Update Coordinator."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the Garmin Connect hub."""
        self.entry = entry
        self.hass = hass
        self.in_china = False

        country = self.hass.config.country
        if country == "CN":
            self.in_china = True

        self._api = Garmin(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], self.in_china)

        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=DEFAULT_UPDATE_INTERVAL
        )

    async def async_login(self) -> bool:
        """Login to Garmin Connect."""
        try:
            await self.hass.async_add_executor_job(self._api.login)
        except (
            GarminConnectAuthenticationError,
            GarminConnectTooManyRequestsError,
        ) as err:
            _LOGGER.error("Error occurred during Garmin Connect login request: %s", err)
            return False
        except (GarminConnectConnectionError) as err:
            _LOGGER.error(
                "Connection error occurred during Garmin Connect login request: %s", err
            )
            raise ConfigEntryNotReady from err
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "Unknown error occurred during Garmin Connect login request"
            )
            return False

        return True

    async def _async_update_data(self) -> dict:
        """Fetch data from Garmin Connect."""
        try:
            summary = await self.hass.async_add_executor_job(
                self._api.get_user_summary, date.today().isoformat()
            )
            _LOGGER.debug(summary)
            body = await self.hass.async_add_executor_job(
                self._api.get_body_composition, date.today().isoformat()
            )
            activities = await self.hass.async_add_executor_job(
                self._api.get_activities_by_date, (date.today()-timedelta(days=7)).isoformat(), (date.today()+timedelta(days=1)).isoformat()
            )


            _LOGGER.debug(body)
            alarms = await self.hass.async_add_executor_job(self._api.get_device_alarms)
            _LOGGER.debug(alarms)
            gear = await self.hass.async_add_executor_job(
                self._api.get_gear, summary[GEAR.USERPROFILE_ID]
            )
            tasks: list[Awaitable] = [
                self.hass.async_add_executor_job(
                    self._api.get_gear_stats, gear_item[GEAR.UUID]
                )
                for gear_item in gear
            ]
            gear_stats = await asyncio.gather(*tasks)
            activity_types = await self.hass.async_add_executor_job(
                self._api.get_activity_types
            )
            gear_defaults = await self.hass.async_add_executor_job(
                self._api.get_gear_defaults, summary[GEAR.USERPROFILE_ID]
            )
            sleep_data = await self.hass.async_add_executor_job(
                self._api.get_sleep_data, date.today().isoformat())

        except (
            GarminConnectAuthenticationError,
            GarminConnectTooManyRequestsError,
            GarminConnectConnectionError,
        ) as error:
            _LOGGER.debug("Trying to relogin to Garmin Connect")
            if not await self.async_login():
                raise UpdateFailed(error) from error
            return {}

        sleep_score = None
        try:
            sleep_score = sleep_data["dailySleepDTO"]["sleepScores"]["overall"]["value"]
        except KeyError:
            _LOGGER.debug("sleepScore was absent")
        summary['lastActivities'] = activities

        return {
            **summary,
            **body["totalAverage"],
            "nextAlarm": alarms,
            "gear": gear,
            "gear_stats": gear_stats,
            "activity_types": activity_types,
            "gear_defaults": gear_defaults,
            "sleepScore": sleep_score,
        }

    async def set_active_gear(self, entity, service_data):
        """Update Garmin Gear settings"""
        if not await self.async_login():
            raise IntegrationError(
                "Failed to login to Garmin Connect, unable to update"
            )

        setting = service_data.data["setting"]
        activity_type_id = next(
            filter(
                lambda a: a[GEAR.TYPE_KEY] == service_data.data["activity_type"],
                self.data["activity_types"],
            )
        )[GEAR.TYPE_ID]
        if setting != SERVICE_SETTING.ONLY_THIS_AS_DEFAULT:
            await self.hass.async_add_executor_job(
                self._api.set_gear_default,
                activity_type_id,
                entity.uuid,
                setting == SERVICE_SETTING.DEFAULT,
            )
        else:
            old_default_state = await self.hass.async_add_executor_job(
                self._api.get_gear_defaults, self.data[GEAR.USERPROFILE_ID]
            )
            to_deactivate = list(
                filter(
                    lambda o: o[GEAR.ACTIVITY_TYPE_PK] == activity_type_id
                    and o[GEAR.UUID] != entity.uuid,
                    old_default_state,
                )
            )

            for active_gear in to_deactivate:
                await self.hass.async_add_executor_job(
                    self._api.set_gear_default,
                    activity_type_id,
                    active_gear[GEAR.UUID],
                    False,
                )
            await self.hass.async_add_executor_job(
                self._api.set_gear_default, activity_type_id, entity.uuid, True
            )
