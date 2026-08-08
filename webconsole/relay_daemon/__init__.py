# -*- coding: utf-8 -*-
"""relay_daemon — 继电器泵冒充云守护进程（跨平台）。

模块：
  tas_io                  Modbus-RTU 驱动（TAS-IO-230R 系列）
  tas_io_ethernet         Windows 冒充云传输（scapy + npcap DHCP）
  linux_cloud_transport   Linux 冒充云传输（dnsmasq DHCP + ip 别名）
  relay_pump_daemon       常驻守护进程主程序（按平台选传输，暴露 HTTP）
"""
