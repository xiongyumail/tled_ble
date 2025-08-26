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
    controller.config_entry = entry  # 保存配置条目引用
    controller.subdevices = entry.options.get("subdevices", {})  # 加载子设备配置
    
    hass.data[DOMAIN][mac] = controller
    
    # 连接到设备
    connected = await controller.connect()
    if not connected:
        _LOGGER.error(f"Failed to connect to {name} at {mac}")
        return False
    
    # 注册配置更新监听
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    
    # 加载平台
    platforms = ["light", "text"]
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    
    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry):
    """处理配置选项更新"""
    await hass.config_entries.async_reload(entry.entry_id)

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