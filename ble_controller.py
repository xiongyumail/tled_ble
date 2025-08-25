# tled_ble/ble_controller.py
import asyncio
import logging
from typing import Optional
from bleak import BleakClient, BleakError
from homeassistant.components.bluetooth import async_get_scanner
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    HEADER,
    CONTROL_CMD
)

_LOGGER = logging.getLogger(__name__)

class TLEDBLEController:
    """控制与TLED BLE设备的连接和通信"""
    
    def __init__(self, hass: HomeAssistant, device_address: str, service_uuid: str, char_uuid: str):
        self.hass = hass
        self.device_address = device_address  # MAC地址
        self.mac_address = device_address
        self.service_uuid = service_uuid      # 服务UUID
        self.char_uuid = char_uuid            # 特征值UUID
        self.name = ""  # 设备名称（从配置中获取）
        self.client: Optional[BleakClient] = None
        self.connected = False
        self.max_retries = 3  # 默认最大重试次数
        self.base_timeout = 15.0  # 基础超时时间（秒）
        self.subdevices = {}  # 存储子设备状态 {地址: {name, state}}

    async def connect(self, timeout: Optional[float] = None, retries: Optional[int] = None) -> bool:
        """连接到BLE设备"""
        timeout = timeout or self.base_timeout
        retries = retries or self.max_retries
        
        for attempt in range(retries):
            try:
                _LOGGER.info(
                    f"尝试连接到设备 {self.device_address}（尝试 {attempt+1}/{retries}），超时时间: {timeout}秒"
                )
                
                # 如果已有客户端实例，先确保断开连接
                if self.client and self.client.is_connected:
                    await self.disconnect()
                
                # 创建新的客户端实例
                self.client = BleakClient(self.device_address)
                
                # 尝试连接
                _LOGGER.debug(f"发起连接请求到 {self.device_address}")
                await self.client.connect(timeout=timeout)
                
                if self.client.is_connected:
                    self.connected = True
                    _LOGGER.info(f"成功连接到设备 {self.device_address}")
                    return True
                
                _LOGGER.warning(f"连接尝试 {attempt+1} 未成功建立连接")
                
            except TimeoutError:
                _LOGGER.warning(
                    f"连接过程超时（尝试 {attempt+1}/{retries}），超时时间: {timeout}秒"
                )
            except BleakError as e:
                _LOGGER.error(
                    f"连接过程发生BLE错误（尝试 {attempt+1}/{retries}）: {str(e)}"
                )
            except Exception as e:
                _LOGGER.exception(
                    f"连接过程发生意外错误（尝试 {attempt+1}/{retries}）: {str(e)}"
                )
            
            # 指数退避重试
            if attempt < retries - 1:
                wait_time = min(5 * (attempt + 1), 30)
                _LOGGER.info(f"等待 {wait_time} 秒后进行下一次连接尝试")
                await asyncio.sleep(wait_time)
        
        self.connected = False
        _LOGGER.error(f"所有 {retries} 次连接尝试均失败")
        return False

    async def disconnect(self) -> None:
        """断开与设备的连接"""
        if self.client and self.client.is_connected:
            try:
                _LOGGER.info(f"断开与设备 {self.device_address} 的连接")
                await self.client.disconnect()
            except Exception as e:
                _LOGGER.error(f"断开连接时发生错误: {str(e)}")
        self.connected = False
        self.client = None

    async def auto_reconnect(self) -> bool:
        """自动重连逻辑"""
        _LOGGER.info(f"启动自动重连到设备 {self.device_address}")
        
        for attempt in range(1, self.max_retries + 1):
            timeout = min(self.base_timeout + (attempt * 5), 60.0)
            if await self.connect(timeout=timeout, retries=1):
                return True
                
            wait_time = min(attempt * 10, 60)
            _LOGGER.info(f"自动重连尝试 {attempt} 失败，将在 {wait_time} 秒后重试")
            await asyncio.sleep(wait_time)
        
        _LOGGER.error(f"所有自动重连尝试均失败，无法连接到 {self.device_address}")
        return False

    async def send_command(self, command: bytes) -> bool:
        """向设备发送原始命令"""
        if not self.connected or not self.client or not self.client.is_connected:
            _LOGGER.warning("发送命令失败：未连接到设备，尝试重连")
            if not await self.auto_reconnect():
                return False
        
        try:
            # 确保没有其他操作在进行
            if self.client.is_connected:
                await self.client.write_gatt_char(self.char_uuid, command)
                _LOGGER.debug(f"成功发送命令到 {self.device_address}: {command.hex()}")
                return True
            return False
        except Exception as e:
            _LOGGER.error(f"发送命令时发生错误: {str(e)}")
            self.connected = False
            return False

    async def send_control_command(self, address: int, is_on: bool, brightness: int) -> bool:
        """发送灯光控制命令（修正：移除校验位，使用0-255亮度范围）"""
        # 直接使用原始亮度值（0-255），无需转换为0-100
        normalized_brightness = brightness if brightness else 0
        
        # 构建命令帧（移除校验位，地址放在命令前）
        cmd_frame = bytearray([
            HEADER,  # 帧头（0xA5）
            (address >> 8) & 0xFF,      # 设备地址高8位（0xFF）
            address & 0xFF,             # 设备地址低8位（0xFF）
            (CONTROL_CMD >> 8) & 0xFF,  # 命令高8位（0x82）
            CONTROL_CMD & 0xFF,         # 命令低8位（0x02）
            0x01 if is_on else 0x00,    # 开关状态（0x01）
            normalized_brightness,      # 亮度（0xFF）
            # 移除校验位（原0x00）
        ])
        
        # 发送命令
        return await self.send_command(cmd_frame)

    async def scan_for_device(self, timeout: float = 10.0) -> bool:
        """扫描设备是否在范围内"""
        try:
            scanner = async_get_scanner(self.hass)
            _LOGGER.info(f"扫描设备 {self.device_address}，超时时间: {timeout}秒")
            
            devices = await scanner.async_discover(timeout)
            found = any(device.address == self.device_address for device in devices)
            
            if found:
                _LOGGER.info(f"在扫描中发现设备 {self.device_address}")
                return True
            else:
                _LOGGER.warning(f"扫描超时，未发现设备 {self.device_address}")
                return False
        except Exception as e:
            _LOGGER.error(f"扫描设备时发生错误: {str(e)}")
            return False

    async def __aenter__(self):
        """异步上下文管理器进入方法"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """异步上下文管理器退出方法"""
        await self.disconnect()