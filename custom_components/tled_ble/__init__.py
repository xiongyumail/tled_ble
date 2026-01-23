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
    
    # 加载子设备配置并确保地址为整数类型
    raw_subdevices = entry.options.get("subdevices", {})
    subdevices = {}
    for k, v in raw_subdevices.items():
        try:
            subdevices[int(k)] = v
        except (ValueError, TypeError):
             _LOGGER.warning(f"Ignored invalid subdevice address: {k}")
    controller.subdevices = subdevices
    
    # 识别网关自身的 Mesh 地址：寻找名称匹配或地址最小的设备作为代理参考
    if subdevices:
        # 优先找名称包含“网关”的，找不到则取第一个
        gateway_addr = next((addr for addr, info in subdevices.items() if "网关" in info["name"]), next(iter(subdevices)))
        controller.gateway_address = gateway_addr
        _LOGGER.info(f"已将 Mesh 地址 0x{gateway_addr:04X} 设为代理网关身份")
    
    hass.data[DOMAIN][mac] = controller
    
    # 连接到设备
    connected = await controller.connect()
    if not connected:
        _LOGGER.error(f"Failed to connect to {name} at {mac}")
        return False
    
    # 注册配置更新监听
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    
    # 加载平台
    platforms = ["light", "text", "sensor"]
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    
    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry):
    """处理配置选项更新，动态同步而不重启集成"""
    mac = entry.data["mac"]
    if mac in hass.data[DOMAIN]:
        controller = hass.data[DOMAIN][mac]
        old_subdevices = set(controller.subdevices.keys())
        
        raw_subdevices = entry.options.get("subdevices", {})
        subdevices = {}
        for k, v in raw_subdevices.items():
            try:
                addr = int(k)
                subdevices[addr] = v
                # 如果是新加的设备（无论是自动发现还是手动输入），触发实体创建
                if addr not in old_subdevices:
                    hass.bus.async_fire(
                        f"{DOMAIN}_new_subdevice_found",
                        {
                            "controller_mac": mac,
                            "address": addr, 
                            "name": v["name"],
                            "state": v.get("state", {"on": False, "brightness": 0})
                        }
                    )
            except (ValueError, TypeError):
                continue
        
        controller.subdevices = subdevices
        _LOGGER.info(f"大王，设备 {mac} 的子设备配置已热更新，新设备已即时受封！")
    # 不再调用 reload，彻底告别断连循环
    # await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    mac = entry.data["mac"]
    controller = hass.data[DOMAIN][mac]
    
    # 断开连接
    if controller.connected and controller.client:
        await controller.disconnect()
    
    # 卸载平台
    platforms = ["light", "text", "sensor"]
    unload_tasks = [
        hass.config_entries.async_forward_entry_unload(entry, platform)
        for platform in platforms
    ]
    results = await asyncio.gather(*unload_tasks)
    unload_ok = all(results)
    
    if unload_ok:
        del hass.data[DOMAIN][mac]
    
    return unload_ok