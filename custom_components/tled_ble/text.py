"""Text entity for TLED BLE debug command input."""
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.text import TextEntity
from homeassistant.const import CONF_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MANUFACTURER
from .ble_controller import TLEDBLEController

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TLED BLE debug text input from config entry."""
    mac = entry.data[CONF_MAC]
    controller = hass.data[DOMAIN][mac]
    async_add_entities([TLEDBLEDebugWrite(controller)])

class TLEDBLEDebugWrite(TextEntity):
    """Text entity to send raw hex commands via BLE."""
    
    def __init__(self, controller: TLEDBLEController):
        self.controller = controller
        self._mac = controller.mac_address
        self._attr_native_value = ""  # 修正属性名
        self._attr_unique_id = f"{self._mac}_debug_write"
        self._attr_name = "协议调试"
        self._attr_pattern = "^[0-9a-fA-F]*$"
        self._attr_pattern_description = "仅允许十六进制字符"

    @property
    def device_info(self) -> DeviceInfo:
        """将调试工具保留在网关代理设备上"""
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            name="网关",
            manufacturer=MANUFACTURER,
            model=f"Mesh 网关 (0x{self.controller.gateway_address:04X})",
        )

    @property
    def icon(self):
        """返回实体图标"""
        return "mdi:console"

    async def async_set_value(self, value: str) -> None:
        """Send input hex command to device."""
        try:
            # Convert hex string to bytes
            data = bytes.fromhex(value)
            success = await self.controller.send_command(data)
            if success:
                _LOGGER.info(f"Command sent: {value}")
                self._attr_native_value = value  # 修正属性名
            else:
                _LOGGER.error(f"Failed to send command: {value}")
        except ValueError as e:
            _LOGGER.error(f"Invalid hex format: {value}, error: {str(e)}")
        self.async_write_ha_state()