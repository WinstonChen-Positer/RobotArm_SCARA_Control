# 机械臂控制程序（DUCO + SCARA）

从多设备控制系统中抽取的独立小程序，只含两个设备控制页：

- **机械臂控制**：DUCO GCR3-618 六轴协作臂（含 DH 夹爪、吸盘泵/阀）
- **SCARA 机械臂**：新松 SA4A-4/0.40（含电磁阀/泵继电器、末端相机）

## 运行环境

- Windows + Python 3.10
- 依赖：PyQt6、thrift、requests、pyyaml、opencv-python、numpy、pyserial、Pillow

## 启动

```bash
python main.py
```

启动后会打开一个两标签页窗口（「机械臂控制」「SCARA 机械臂」），并自动拉起两个本地后端服务：

- 继电器守护进程（127.0.0.1:19100）—— 泵/阀/信号灯的下发后端
- armweb 机械臂代理（127.0.0.1:8080）—— DUCO 真机控制通道

若这两个服务已由人工启动，可加 `--no-services` 跳过自启：

```bash
python main.py --no-services
```

程序退出时会自动停掉由它启动的服务（外部已运行的服务不受影响）。服务日志在 `logs/` 目录。

## DUCO 真机连接

连接链路：本页面 → HTTP → 本机 armweb（127.0.0.1:8080）→ thrift → 真机 192.168.1.10:7003。

1. 确认 armweb 已在运行（默认随 `main.py` 自启）。手工启动方式：

   ```bash
   python webconsole/server.py --port 8080 --host 127.0.0.1
   ```

2. 在「机械臂控制」页点连接，服务器地址填 `http://127.0.0.1:8080`（默认值），由 armweb 侧连真机 192.168.1.10:7003。
3. **运动前须把操作模式切到自动**：在界面上切换，或 `POST http://127.0.0.1:8080/api/switch_mode`，Body `{"mode": 1}`。手动模式下运动命令会被拒绝。
4. armweb 自带浏览器控制台：打开 `http://127.0.0.1:8080/` 可查看状态/相机画面。

## SCARA 真机连接

连接链路：本页面 → 命令行桥 `snrobot.exe` → 控制器 192.168.1.100:20002。

1. 确认桥程序位于 `C:\software\SNRobotLab\snrobot.exe`。该目录内含 RobotCommunication SDK 与许可证 `SiaSunRobot.lic`，**exe 必须与许可证同目录运行**，不能单独拷走。
2. 在「SCARA 机械臂」页点连接。连接参数（桥路径、控制器地址、超时等）可在程序根目录建 `scara_config.toml` 覆盖，缺省即上述值。
3. 泵/电磁阀按钮走继电器守护进程（127.0.0.1:19100，随 `main.py` 自启；也可手工启动：`python webconsole/relay_daemon/relay_pump_daemon.py --http-port 19100`）。

## 安全提示

- 首次运动务必低速（速度比例调小），急停按钮保持在手边。
- DUCO 碰撞报警后需在示教器上复位，软件无法清除。
- SCARA 运动前确认已使能、模式正确（T1 示教限速，T2 全速默认禁用）。
- 页面上的使能（伺服上电）会让机械臂带电，属于需谨慎确认的动作。

## 目录说明

```
main.py                 启动器（两标签页 + 本地服务自启）
src/
  core/                 模块接口定义
  utils/                日志、本地服务管理
  design_system.py      全局设计令牌（色板/字号/QSS）
  robot_arm/            DUCO 控制页（UI）
  devices/robot_arm/    DUCO 后端（http/thrift/sim、RPC、夹爪、安全、点位）
  scara/                SCARA 模块（UI/控制器/标定/视觉/取放序列）
webconsole/
  server.py             armweb 机械臂代理（HTTP → thrift → 真机）
  pump_controller.py    继电器守护进程 HTTP 客户端
  index.html            armweb 浏览器控制台页面
  relay_daemon/         继电器守护进程（Modbus → 塔石 IO 板）
```

说明：

- `src/devices/robot_arm/calib/joint_replay/`（自动流程专用的关节回放数据）未随本程序打包，两个页面本身不读它。
- SCARA 送检取放序列中的"切换物镜倍率"步骤依赖显微镜预设文件（`presets/*.json`），本程序不含显微镜模块，该步骤会跳过并报缺文件提示，其余步骤不受影响。
- 相机画面为懒加载（首次打开时 import cv2），无相机不影响机械臂控制。
