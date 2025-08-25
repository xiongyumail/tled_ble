# tled_ble/__init__.py
"""The TLED BLE integration."""
import logging
import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .ble_controller import TLEDBLEController

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the TLED BLE component."""
    hass.data.setdefault(DOMAIN, {})
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up TLED BLE from a config entry."""
    mac = entry.data["mac"]
    name = entry.data["name"]
    service_uuid = entry.data["service_uuid"]
    char_uuid = entry.data["char_uuid"]
    
    # 创建BLE控制器实例
    controller = TLEDBLEController(hass, mac, service_uuid, char_uuid)
    controller.name = name
    hass.data[DOMAIN][mac] = controller
    
    # 连接到设备
    connected = await controller.connect()
    if not connected:
        _LOGGER.error(f"Failed to connect to {name} at {mac}")
        return False
    
    # 加载平台
    platforms = ["light", "text"]
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    mac = entry.data["mac"]
    controller = hass.data[DOMAIN][mac]
    
    # 断开连接
    if controller.connected and controller.client:
        await controller.disconnect()
    
    # 卸载平台
    platforms = ["light", "text"]
    unload_tasks = [
        hass.config_entries.async_forward_entry_unload(entry, platform)
        for platform in platforms
    ]
    results = await asyncio.gather(*unload_tasks)
    unload_ok = all(results)
    
    if unload_ok:
        del hass.data[DOMAIN][mac]
    
    return unload_ok