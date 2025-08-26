# tled_ble/const.py
"""TLED BLE集成的常量定义"""
DOMAIN = "tled_ble"
MANUFACTURER = "TLED"

# 设备筛选前缀
DEVICE_NAME_PREFIX = "TLED"

# 命令格式常量
HEADER = 0xA5
CONTROL_CMD = 0x8202

# 广播地址（控制所有设备）
BROADCAST_ADDR = 0xFFFF