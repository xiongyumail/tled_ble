# tled_ble/config_flow.py
import logging
import asyncio
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlow, ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.const import CONF_NAME, CONF_MAC
from homeassistant.data_entry_flow import FlowResult
from bleak import BleakScanner, BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic

from .const import DOMAIN, MANUFACTURER, DEVICE_NAME_PREFIX

_LOGGER = logging.getLogger(__name__)

class TLEDBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """TLED BLE设备的配置流程，支持扫描和信号强度显示"""
    
    VERSION = 1
    SCAN_DURATION = 10  # 扫描持续时间（秒）
    discovered_devices = []  # 存储发现的设备列表
    selected_device = None  # 选中的设备
    device_services = {}    # 设备的服务和特征值

    async def async_step_user(self, user_input=None) -> FlowResult:
        """初始步骤：选择配置方式"""
        if user_input is not None:
            if user_input["setup_method"] == "scan":
                return await self.async_step_scan()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("setup_method", default="scan"): vol.In({
                    "scan": "自动扫描设备（推荐）",
                    "manual": "手动输入设备信息"
                })
            })
        )

    async def async_step_scan(self, user_input=None) -> FlowResult:
        """扫描并显示BLE设备，包含信号强度"""
        # 如果用户已选择设备，处理选择结果
        if user_input is not None:
            selected_mac = user_input["device"]
            # 查找选中的设备
            self.selected_device = next(
                (dev for dev in self.discovered_devices if dev.address == selected_mac),
                None
            )
            
            if self.selected_device:
                # 扫描设备的服务UUID
                return await self.async_step_select_service()

        # 开始扫描设备
        self.discovered_devices = []
        try:
            _LOGGER.info(f"开始扫描BLE设备，持续{self.SCAN_DURATION}秒...")
            
            # 扫描并收集设备信息（包含RSSI）
            devices = await BleakScanner.discover(
                timeout=self.SCAN_DURATION,
                return_adv=True  # 返回广告数据以获取RSSI
            )
            
            # 筛选TLED设备并提取RSSI
            for device, adv_data in devices.values():
                # 筛选名称包含特定前缀的设备
                if device.name and DEVICE_NAME_PREFIX.lower() in device.name.lower():
                    self.discovered_devices.append(device)
                    _LOGGER.info(
                        f"发现TLED设备: {device.name} ({device.address}), "
                        f"信号强度: {adv_data.rssi} dBm"
                    )

            # 如果未发现设备
            if not self.discovered_devices:
                return self.async_show_form(
                    step_id="scan",
                    errors={"base": "no_devices_found"},
                    description_placeholders={
                        "duration": self.SCAN_DURATION
                    }
                )

            # 构建设备选择列表，包含信号强度
            device_options = []
            for device in sorted(
                self.discovered_devices, 
                key=lambda d: devices[d.address][1].rssi, 
                reverse=True  # 按信号强度从强到弱排序
            ):
                rssi = devices[device.address][1].rssi
                # 根据信号强度显示不同指示
                if rssi >= -50:
                    signal_strength = "📶 强"
                elif rssi >= -70:
                    signal_strength = "📶 中"
                else:
                    signal_strength = "📶 弱"
                    
                device_options.append(
                    (device.address, 
                     f"{device.name or 'Unknown TLED Device'} "
                     f"({device.address}) - {signal_strength} ({rssi} dBm)")
                )

            # 显示设备选择表单
            return self.async_show_form(
                step_id="scan",
                data_schema=vol.Schema({
                    vol.Required("device"): vol.In(dict(device_options))
                }),
                description_placeholders={
                    "count": len(self.discovered_devices)
                }
            )

        except Exception as e:
            _LOGGER.error(f"扫描设备时出错: {str(e)}")
            return self.async_show_form(
                step_id="scan",
                errors={"base": "scan_failed"}
            )

    async def async_step_select_service(self, user_input=None) -> FlowResult:
        """选择设备的Service和Characteristic UUID（支持动态更新特征值）"""
        if user_input is not None:
            # 检查是否仅选择了服务（需要更新特征值列表）
            selected_service = user_input["service_uuid"]
            
            # 如果用户刚选择完服务，重新渲染表单以更新特征值选项
            if "char_uuid" not in user_input or user_input["char_uuid"] not in [
                char["uuid"] for char in self.device_services.get(selected_service, [])
            ]:
                # 获取选中服务的特征值
                char_options = [(char["uuid"], f"Characteristic: {char['uuid']} ({char['properties']})") 
                            for char in self.device_services.get(selected_service, [])]
                
                # 默认选择第一个特征值
                first_char = char_options[0][0] if char_options else ""
                
                return self.async_show_form(
                    step_id="select_service",
                    data_schema=vol.Schema({
                        vol.Required("service_uuid", default=selected_service): vol.In(
                            {uuid: f"Service: {uuid}" for uuid in self.device_services.keys()}
                        ),
                        vol.Required("char_uuid", default=first_char): vol.In(dict(char_options))
                    }),
                    description_placeholders={"device": self.selected_device.address}
                )

            # 如果已选择有效特征值，创建配置条目
            await self.async_set_unique_id(self.selected_device.address)
            self._abort_if_unique_id_configured()
            
            device_name = self.selected_device.name or f"TLED Device {self.selected_device.address[-5:]}"
            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_MAC: self.selected_device.address,
                    CONF_NAME: device_name,
                    "service_uuid": user_input["service_uuid"],
                    "char_uuid": user_input["char_uuid"]
                }
            )
        
        # 初始加载：获取所有服务并显示第一个服务的特征值
        try:
            # 修正设备地址格式，移除可能的前缀（如'dev_'）
            device_address = self.selected_device.address
            if device_address.startswith('dev_'):
                device_address = device_address.replace('dev_', '').replace('_', ':')
            
            # 使用修正后的地址连接设备
            async with BleakClient(device_address) as client:
                _LOGGER.info(f"已连接到 {device_address}，正在扫描服务...")
                services = client.services
                self.device_services = {}
                
                for service in services:
                    characteristics = []
                    for char in service.characteristics:
                        if char.properties:
                            props = ",".join(char.properties)
                            characteristics.append({"uuid": char.uuid, "properties": props})
                    if characteristics:
                        self.device_services[service.uuid] = characteristics
            
            if not self.device_services:
                return self.async_show_form(
                    step_id="select_service",
                    errors={"base": "no_services_found"}
                )
            
            # 服务选项
            service_options = {uuid: f"Service: {uuid}" for uuid in self.device_services.keys()}
            first_service = next(iter(self.device_services.keys()))
            
            # 初始特征值选项（第一个服务）
            char_options = [(char["uuid"], f"Characteristic: {char['uuid']} ({char['properties']})") 
                        for char in self.device_services[first_service]]
            first_char = char_options[0][0] if char_options else ""
            
            return self.async_show_form(
                step_id="select_service",
                data_schema=vol.Schema({
                    vol.Required("service_uuid", default=first_service): vol.In(service_options),
                    vol.Required("char_uuid", default=first_char): vol.In(dict(char_options))
                }),
                description_placeholders={"device": self.selected_device.address}
            )
            
        except Exception as e:
            _LOGGER.error(f"获取设备服务时出错: {str(e)}")
            return self.async_show_form(
                step_id="select_service",
                errors={"base": "service_scan_failed"}
            )

    async def async_step_manual(self, user_input=None) -> FlowResult:
        """手动输入设备信息的步骤"""
        if user_input is not None:
            mac = user_input[CONF_MAC].upper()
            if not self._is_valid_mac(mac):
                return self.async_show_form(
                    step_id="manual",
                    errors={"base": "invalid_mac"}
                )
            
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data={
                    CONF_MAC: mac,
                    CONF_NAME: user_input[CONF_NAME],
                    "service_uuid": user_input["service_uuid"],
                    "char_uuid": user_input["char_uuid"]
                }
            )
            
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_MAC, description="设备MAC地址 (如 AA:BB:CC:DD:EE:FF)"): str,
                vol.Required("service_uuid", description="服务UUID"): str,
                vol.Required("char_uuid", description="特征值UUID"): str
            })
        )

    @staticmethod
    @callback
    def _is_valid_mac(mac: str) -> bool:
        """验证MAC地址格式"""
        try:
            mac_clean = mac.replace(":", "").replace("-", "")
            return len(mac_clean) == 12 and all(c in "0123456789ABCDEF" for c in mac_clean)
        except:
            return False

    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """创建选项流程用于管理子设备"""
        return TLEDBLEOptionsFlow(config_entry)


class TLEDBLEOptionsFlow(config_entries.OptionsFlow):
    """TLED BLE设备的选项配置流程，用于管理子设备"""
    
    def __init__(self, config_entry: ConfigEntry):
        self.subdevices = config_entry.options.get("subdevices", {})

    async def async_step_init(self, user_input=None) -> FlowResult:
        """管理子设备列表"""
        errors = {}
        
        if user_input is not None:
            # 解析用户输入的子设备列表
            new_subdevices = self.subdevices.copy()
            raw_entries = user_input.get("subdevices", "").strip().split("\n")
            for entry in raw_entries:
                if not entry.strip():
                    continue
                try:
                    name, addr_str = entry.split(":", 1)
                    name = name.strip()
                    addr = int(addr_str.strip(), 16)  # 转换为十六进制整数
                    
                    if not name:
                        raise ValueError("名称不能为空")
                    if addr < 0x0001 or addr > 0xFF00:
                        raise ValueError("地址必须是0x0001-0xFF00之间的十六进制数")
                    
                    # 关键：保留现有状态（如果存在），仅更新名称
                    existing_state = new_subdevices.get(addr, {}).get("state", {"on": False, "brightness": 0})
                    new_subdevices[addr] = {
                        "name": name,
                        "state": existing_state
                    }
                except ValueError as e:
                    errors["base"] = f"格式错误: {str(e)} (正确格式: 名称:十六进制地址，如 电视柜:0003)"
                    break
            
            if not errors:
                self.subdevices = new_subdevices
                # 保存配置并重新加载集成
                return self.async_create_entry(
                    title="",
                    data={"subdevices": self.subdevices}
                )

        # 格式化现有子设备为文本显示
        subdevices_text = "\n".join(
            [f"{info['name']}:{int(addr, 16):04X}" if isinstance(addr, str) else f"{info['name']}:{addr:04X}" 
            for addr, info in self.subdevices.items()]
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    "subdevices", 
                    default=subdevices_text,
                    description={
                        "help": "每行输入一个子设备，格式：名称:十六进制地址（如 电视柜:0003）\n"
                                "地址范围：0001-FF00\n"
                                "⚠️ 新增设备时，可直接追加到现有条目后（无需重新输入旧设备）\n"
                                "提示：按 Enter 键可换行添加多个设备"
                    }
                ): str  # 使用原生str类型，依赖用户手动换行
            }),
            errors=errors
        )