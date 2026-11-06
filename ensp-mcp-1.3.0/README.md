# eNSP MCP

[![Tests](https://github.com/Heaxu/ensp-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/Heaxu/ensp-mcp/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

给华为 eNSP 用的一站式 MCP：**搭拓扑**和**配设备**在同一个工具集里完成。

An unofficial MCP server for building Huawei eNSP topologies and configuring
lab devices through their console ports.

当前版本 **1.3.0**，共 19 个 MCP 工具。本版新增**多设备并发**（`device_batch`）
和**实验自动验收**（`lab_verify`）：一次并发下发到多台设备，并按规则判定实验
是否真的做对了，给出 PASS / FAIL 结论。详见 [更新记录](CHANGELOG.md) 与
[开发及验收说明](docs/DEVELOPMENT.md)。

拓扑侧全是离线的文件操作，不用开 eNSP；设备侧走 eNSP 的 console 端口，
要求 eNSP 已启动、目标设备已开机。

## 能干什么

- 按型号加设备、按接口名连线，接口序号和 `.topo` 的 XML 细节都不用管
- 自动分层布局，自动分配 console 端口，自动生成 MAC 和 AP 序列号
- 用 eNSP 原生背景矩形和文字创建分区，自动给标题、设备名和分区留出间距
- 交给 eNSP 之前检查拓扑结构，以及 VirtualBox、Hyper-V/VBS、模板机和镜像
- 可选渲染 PNG 辅助预览；拓扑创建和验收不依赖预览图
- 导出 Mermaid / draw.io / CSV，直接拿去写文档
- telnet 进设备批量下发配置、读回实配做对比、跑 ping 和各种 display

## 安装

基本环境：Python 3.10 或更高版本。实际运行 eNSP 和连接设备需要 Windows 与
已安装的 eNSP；使用 AR、WLAN、NE/CE 或 USG6000V 时，还需要与 eNSP 版本
匹配的 VirtualBox、模板机或设备镜像。仓库不提供这些厂商软件。

```powershell
git clone https://github.com/Heaxu/ensp-mcp.git
cd ensp-mcp
python -m pip install -e .
```

也可以从 GitHub Releases 下载 wheel 后安装：

```powershell
python -m pip install .\ensp_mcp-1.3.0-py3-none-any.whl
```

在 MCP 客户端配置文件中注册：

```json
{
  "mcpServers": {
    "ensp-mcp": {
      "command": "ensp-mcp"
    }
  }
}
```

也可以用 Python 解释器启动，便于切换虚拟环境：`python -m ensp_mcp.server`。
开发更新后要在客户端重启 MCP 服务，已运行的进程不会自动加载新源码。

环境自检：`python -m ensp_mcp.doctor`。给定拓扑并把运行前置条件作为退出状态：
`python -m ensp_mcp.doctor --topo lab.topo --strict-runtime`。运行测试：
`python -B -m unittest discover -s tests -t . -v`。

## 典型流程

```
environment_check    先检查 eNSP、VirtualBox、模板机和镜像
list_models          看型号、接口和选择建议；普通防火墙默认 USG5500
topo_create          建一个空拓扑
topo_add_devices     批量加设备
topo_connect         批量连线
topo_layout          自动摆开；需要时生成分区背景和标题
topo_validate        检查
topo_render          （可选）出辅助预览图
  ↓ 在 eNSP 里打开，全选启动设备
device_push_config   灌配置
device_probe         ping 一下验证
```

## 工具

### 设备库

| 工具 | 干什么 |
|---|---|
| `list_models` | 列出 43 种型号、接口、默认选择和启动风险。给 `topo_path` 还能核对内置表和实际 eNSP 版本有没有出入 |

没有明确型号要求时，`categoryDefaults` 中的防火墙默认值是 `USG5500`。
显式传入 `USG6000V` 仍会严格按该型号创建，同时返回外部镜像和启动风险提示，
不会静默换成端口结构不同的型号。

### 拓扑

| 工具 | 干什么 |
|---|---|
| `topo_create` | 新建空 `.topo` |
| `topo_inspect` | 读出设备、链路、每台设备的空闲接口和 console 端口 |
| `topo_add_devices` | 批量加设备，支持 `count` 批量克隆和终端 IP 预设 |
| `topo_connect` | 批量连线，接口可写名字也可省略让它自动挑空闲口 |
| `topo_remove` | 删设备或断连线 |
| `topo_layout` | `layered` / `tree` / `grid` / `ring` 四种普通布局，也可按 `zones` 生成原生分区背景和标题 |
| `topo_validate` | 重名、接口越界、一口多连、console 冲突、孤立设备、图标/文字/分区碰撞和条件型号风险 |
| `topo_render` | 可选的 Pillow PNG 辅助示意图 |
| `topo_export` | 导出 Mermaid / draw.io / CSV |

分区示例：

```json
{
  "path": "D:/lab/campus.topo",
  "zones": [
    {"title": "总部核心区", "devices": ["Core1", "Core2", "FW1"], "color": "#E8F1FF"},
    {"title": "办公区", "devices": ["Access1", "PC1", "PC2"], "color": "#EAF8EE"}
  ]
}
```

分区布局使用 eNSP 原生 `shape`/`txttip` 字段，按设备名宽度计算单元格，并给
背景框、标题栏和相邻分区预留固定间距。未列出的设备默认进入“未分区”。
eNSP 的标注没有稳定 ID，因此拓扑已有手工图形或文字时默认拒绝覆盖；确实要
全部重建时显式传 `replace_annotations=true`。普通布局遇到已有标注也默认拒绝
移动设备，避免背景框留在旧坐标。

### 启动环境

| 工具 | 干什么 |
|---|---|
| `environment_check` | 只读检查 eNSP、VirtualBox 版本和硬件虚拟化、Hyper-V/VBS 冲突、Host-Only 网卡、AR/WLAN 基机、SVRP 与 USG6000V 镜像 |

传入 `path` 后查看 `readyForTopology` 和每个 `modelChecks[].blockers`；还没建图时
也可传 `models=["AR2220", "USG5500"]` 预检计划型号。CLI 对应参数可重复写，
例如 `python -m ensp_mcp.doctor --model AR2220 --model USG5500 --strict-runtime`。
该检查不会启动虚拟机，不会关闭 Windows 功能，也不会注册或修改镜像。

eNSP 1.3.00.200T 优先使用经验证的 VirtualBox 5.2.x。VirtualBox 驱动处于
Running 并不能证明设备可启动：如果 Hyper-V/VBS 占用 AMD-V/VT-x，老版本
VirtualBox 仍会报 raw-mode 错误。诊断会把这种情况单独报为
`VBOX_HYPERV_CONFLICT`。

`USG6000V` 是条件使用型号，通常要另行安装并注册 `vfw_usg.vdi`；只有实验明确
要求该型号或其独有能力时再选。一般防火墙实验优先 `USG5500`，它使用 eNSP
自带的本地模拟器和固件。`NE40E`、`CE6800`、`CE12800` 还依赖已注册的
SVRP 镜像。

### 设备配置

`target` 既接受设备名（如 `Core1`，需要配 `topo_path`），也接受 console
端口号（如 `2002`）。

| 工具 | 干什么 |
|---|---|
| `device_exec` | 执行任意命令并返回回显 |
| `device_push_config` | 灌整份配置，可以来自文本或 `.cfg` 文件 |
| `device_fetch_config` | 读回运行配置，可存盘，可和基线逐行对比 |
| `device_probe` | ping / tracert / 接口状态 / ARP / MAC / 路由 / VLAN / OSPF |
| `device_sessions` | 查看和关闭保持中的 console 连接 |

### 多设备并发

| 工具 | 干什么 |
|---|---|
| `device_batch` | 对多台设备并发执行命令或下发各自的配置，单台失败不影响其他 |

`devices` 省略表示拓扑里所有带 console 的设备，也可以混写设备名、设备类别
（`AR` / `LSW` / `FW` / `WLAN` / `CLIENT`）和 `all`。两种模式二选一：

- `commands`：同一组命令打到所有选中设备，适合批量查询或统一配置片段。
- `configs`：`{"Core1": "...", "Core2": "..."}`，每台一份配置，`save` 默认开启。

`concurrency` 默认 4、范围 1–16。返回每台设备的 `ok`、执行条数、错误数、
失败命令和 `durationMs`；完整回显默认不回传，需要时设 `include_results=true`。
批量里的命令和单台工具一样自动留痕，同一次调用归为同一个 `batch`。

### 实验自动验收

| 工具 | 干什么 |
|---|---|
| `lab_verify` | 按规则跑一组检查，判定实验达标与否，给出 PASS / FAIL |

一条检查项 = 某台设备上跑一条命令 + 对回显的判定规则。判定规则可组合：
`contains` / `notContains`、`regex` / `notRegex`、
`minCount{"pattern","atLeast"}`、`reachable`（ping 是否通）、`maxLoss`（丢包上限）。
`expect` 留空表示“命令跑通就算过”。命令本身报错一律判 FAIL。

内置套件：`reachable`（逐台登录体检）、`fw_ha`（HRP 角色 + 心跳口 + VRRP 主备）、
`bfd`（会话 Up）、`ospf`（邻居 Full）、`vrrp`、`vlan`。

```json
{
  "topo_path": "D:/work/fw-ha.topo",
  "suite": "fw_ha",
  "save_report": "D:/work/fw-ha-verify.json"
}
```

自定义检查项的 `target` 可以是设备名、设备类别或 `all`，用类别时自动展开到
该类所有设备；拓扑里没有这类设备就记为 SKIP 而不是 FAIL：

```json
{
  "topo_path": "D:/work/fw-ha.topo",
  "checks": [
    {"name": "FW1 是主用", "target": "FW1", "command": "display hrp state",
     "expect": {"regex": "Role\\s*:\\s*active"}},
    {"name": "内外网可达", "target": "Core1", "command": "ping -c 5 10.1.1.1",
     "expect": {"reachable": true, "maxLoss": 0}}
  ]
}
```

返回 `verdict`、`passed` / `failed` / `skipped` 计数，以及每条检查的状态、
未满足的条件和回显片段（`include_output` 可关）。验收跑过的命令同样进入命令
留痕，`tool` 记为 `lab_verify`，可以在 `command_trace` 的报告里和配置下发改在
一起对照。

### 命令留痕可视化

| 工具 | 干什么 |
|---|---|
| `command_trace` | 把“哪条命令打到了哪台设备”生成可交互 HTML 报告 |

`device_exec`、`device_push_config`、`device_fetch_config`、`device_probe` 的每条命令
（含 `save`）都会自动留痕，追加到 `%USERPROFILE%\.ensp-mcp\commands.jsonl`
（环境变量 `ENSP_MCP_LOG_DIR` 可改目录，超过 8 MB 自动轮转）。
`command_trace` 默认只看最近一次用过的拓扑，按 `.topo` 真实坐标画拓扑，
节点角标显示命令数和失败数，点节点跳到该设备的命令明细（时间、来源工具、
状态和可展开回显）。报告默认写到拓扑同目录的 `<拓扑名>-commands.html`。

所有连设备的工具（`device_exec`、`device_push_config`、`device_fetch_config`、
`device_probe`、`device_batch`、`lab_verify`）都支持 `username`、`password`、
`new_password`。
无需认证时省略；仅密码的 console 可只给 `password`；`new_password` 仅在
设备要求首次设置或修改密码时使用，不会主动触发改密。

**执行结果。** 检查返回的 `ok`、逐条 `results` 和 `saveStatus`。
`device_exec` / `device_push_config` 默认 `stop_on_error=true`，遇错停止；
出错后不自动保存，也不自动回滚已生效的配置。超时或断线会关闭连接并报告
明确错误，不会自动重放命令。`saveStatus.status=saved` 才表示识别到保存成功。

**整份配置。** `device_push_config` 支持以 `#` 分段的运行配置，每个分段
回到系统视图后再执行。`device_fetch_config` 的差异包含 `unifiedDiff`，
保留上下文、行顺序及重复项。

**探测结果。** ping 结果中的 `parsed.reachable` 才表示是否连通；`null`
表示回显不足，不能判断。其他 display 查询目前返回原始回显。

## 几个说明

**接口命名。** 交换机从 1 开始（`GigabitEthernet0/0/1`），路由器和 AP 从 0
开始（`GigabitEthernet0/0/0`）。写 `GE0/0/1`、`Gi0/0/1`、
`GigabitEthernet0/0/1` 都认，也可以直接写扁平序号。

**console 端口。** eNSP 把有 console 的设备映射到本机 2000 起的 TCP 端口。
PC、Server、STA、Cloud、HUB 这些没有 console，只能在 eNSP 界面里双击配置。

**Cloud。** 它的接口是在 eNSP 界面里手工添加并绑定真实网卡的，`.topo` 里
接口数写作 0。连线时按序号给（0、1、2……），校验会提醒你去界面里补配置。

**设备开关机。** eNSP 启动部分设备时会动态克隆 VirtualBox 模板机，实例名
不固定。MCP 先做只读预检，最终启动仍在 eNSP 界面完成；预检通过也不等同于
镜像已经完成真实启动验收。

**文件格式。** eNSP 写出的 `.topo` 声明 `encoding="UNICODE"` 但实际是
UTF-8、CRLF 换行。本工具按同样的形态写回，读的时候两种编码都认。

编辑已有拓扑时保留标注、扩展属性和实际板卡 XML，使用原子写入；若文件在
读取后被其他程序修改，会拒绝覆盖并要求重新读取。避免同时在 eNSP 和 MCP
中保存同一文件。读取兼容 UTF-8、UTF-16 以及官方中文样例使用的 CP936/
GB18030。接口与线型校验不替代真实设备的启动和连通性验收。

`topo_render` 是 Pillow 生成的示意图，并非从 eNSP 导出的截图；它不验证真实
界面、镜像或启动状态。Mermaid/draw.io/CSV 导出主要表达设备和链路，不保证
原生分区标注的视觉等价。

## 小规模端到端验收

```powershell
python examples/e2e_lab.py create --directory ./work/e2e-lab
# 在 eNSP 中打开生成的 acceptance.topo 并启动两台交换机
python examples/e2e_lab.py run --directory ./work/e2e-lab --apply
# 手动重启两台设备后，只读复验保存和连通性
python examples/e2e_lab.py run --directory ./work/e2e-lab
```

脚本会保留实际 MCP 结果和读回配置。自动测试的 Telnet 服务是协议测试桩，
不能替代真实 eNSP 设备测试，也不表示 43 种型号均已完成联调。

## 开源许可与声明

本项目代码采用 [MIT License](LICENSE)。项目是社区维护的非官方工具，与华为
及 eNSP 官方没有隶属或授权关系。Huawei、eNSP 及相关产品名称归各自权利人
所有。

仓库不包含 eNSP、VirtualBox、USG6000V/SVRP 镜像或其他厂商软件。请从合法
来源自行取得所需软件和镜像，并遵守相应许可。问题和改进建议可提交到
[GitHub Issues](https://github.com/Heaxu/ensp-mcp/issues)。
