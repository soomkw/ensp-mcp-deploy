# eNSP MCP 一键部署包 3.0

在 Windows 上一条命令部署 **ensp-mcp 1.3.0**，并自动注册到 **Claude Code** 和 **WorkBuddy**。

> **3.0 新增：命令留痕可视化 + 多设备并发 + 实验自动验收。**
> 用 MCP 配设备时，每台设备收到的每条命令都会自动记录；`command_trace` 生成 HTML 报告
> （左边拓扑图带命令数/失败数角标，右边按设备列明细，带时间、来源、✓/✕ 和可展开回显）。
> `device_batch` 一次并发下发到多台设备（默认 4 台同时，最大 16），单台失败不影响其他；
> `lab_verify` 按规则判定实验是否真的做对了，直接给 PASS / FAIL。
> 想先看效果，双击 `示例\命令留痕示例.html`。

---

## 一、一键部署（每台电脑都一样）

1. 把整个 `大牛逼文件夹3.0` 拷到目标电脑任意位置（路径含中文、空格都没问题）
2. 双击 **`一键部署.bat`**，等它跑完
3. 重启客户端：
   - **Claude Code**：`/mcp` 里看 `ensp-mcp` 是否 ✔ Connected
   - **WorkBuddy**：左下角 `+` → 连接器管理 → 自定义连接器 → 对 `ensp-mcp` 点**信任**

脚本自动完成：

| 步骤 | 内容 |
|---|---|
| 1 | 检测 Python ≥ 3.10，没有就静默安装本包的 Python 3.12.10（仅当前用户，不改系统 PATH） |
| 2 | 在 `%USERPROFILE%\.venvs\ensp-mcp` 建独立虚拟环境（已存在则复用） |
| 3 | 结束占用旧版本的进程、清掉上次中断的残留，然后从 `wheelhouse` 离线安装 `ensp-mcp 1.3.0` |
| 4 | 检查/创建 VirtualBox Host-Only 网卡（缺失时仅这一步弹 UAC 提权） |
| 5 | 运行环境自检 `python -m ensp_mcp.doctor` |
| 6 | 注册到 Claude Code（有 `claude` 命令时） |
| 7 | 合并写入 `%USERPROFILE%\.workbuddy\mcp.json`（已有的其他 MCP 配置会保留） |

> wheel 缓存按 **Windows x64 + Python 3.12** 准备。目标机若是 Python 3.10/3.11，
> 离线安装会失败，脚本自动改用**源码目录**联网安装（`ensp-mcp-1.3.0\`）。

---

## 二、目录内容

| 文件/目录 | 说明 |
|---|---|
| `一键部署.bat` | 双击入口 |
| `deploy.ps1` | 部署脚本主体（Python 检测 → venv → 安装 → 网卡 → 自检 → 注册） |
| `python-3.12.10-amd64.exe` | 官方 Python 安装包（用户级静默安装，26 MB） |
| `wheelhouse\` | 全部依赖 wheel + `ensp_mcp-1.3.0` wheel（离线安装用，约 23 MB） |
| `ensp-mcp-1.3.0\` | MCP 完整源码（可读、可改；改完 `pip install -e .` 或重跑脚本） |
| `ensp-mcp-1.3.0.zip` | 源码压缩包（备份/分发用） |
| `示例\命令留痕示例.html` | 可视化报告样例（15 台设备 / 43 条命令） |
| `示例\验收报告示例.json` | `lab_verify` 输出样例（含 PASS / FAIL 与未满足条件） |
| `示例\demo-campus.topo` | 样例拓扑，可配合示例报告对照 |

---

## 三、使用流程

```
environment_check → list_models → topo_create → topo_add_devices
→ topo_connect → topo_layout → topo_validate
→ （在 eNSP 里打开拓扑、全选启动设备）
→ device_batch / device_push_config / device_exec / device_probe
→ lab_verify      ← 判定配置是否真的生效（PASS / FAIL）
→ command_trace   ← 生成命令留痕可视化报告
```

### `device_batch` 并发下发

```json
{"topo_path": "D:/work/lab.topo",
 "configs": {"FW1": "sysname FW1\n...", "FW2": "sysname FW2\n..."},
 "concurrency": 4}
```

`devices` 省略 = 拓扑里所有带 console 的设备，也可以写设备名、类别（`AR`/`LSW`/`FW`/`WLAN`/`CLIENT`）
或 `all`。`commands` 和 `configs` 二选一：前者给同一组命令，后者每台一份配置（`save` 默认开）。
返回每台的 `ok`、执行条数、错误数、失败命令和 `durationMs`，完整回显要 `include_results=true` 才带。

### `lab_verify` 自动验收

```json
{"topo_path": "D:/work/lab.topo", "suite": "fw_ha",
 "save_report": "D:/work/lab-verify.json"}
```

内置套件：`reachable`（逐台登录体检）、`fw_ha`（HRP 角色 + 心跳口 + VRRP 主备）、
`bfd`、`ospf`、`vrrp`、`vlan`。也可以给 `checks` 自定义检查项，
`expect` 支持 `contains` / `notContains` / `regex` / `notRegex` /
`minCount{"pattern","atLeast"}` / `reachable` / `maxLoss`。
`target` 用设备类别时自动展开到该类所有设备；拓扑里没有这类设备记 SKIP 而不是 FAIL。

`command_trace` 参数：

| 参数 | 说明 |
|---|---|
| `path` | `.topo` 路径；省略用最近一次用过的拓扑 |
| `device` | 只看某台设备，例如 `"FW1"` |
| `out_path` | 报告输出位置，默认是拓扑同目录的 `<拓扑名>-commands.html` |
| `limit` | 最多取最近多少条命令，默认 2000（1–20000） |
| `show_output` | 明细里是否带命令回显，默认带 |
| `open_browser` | 生成后自动用默认浏览器打开，默认不打开 |

留痕日志：`%USERPROFILE%\.ensp-mcp\commands.jsonl`（一条命令一行，超过 8 MB 自动轮转为 `.jsonl.1`；
可用环境变量 `ENSP_MCP_LOG_DIR` 改目录）。留痕是自动的，不需要额外开关。

---

## 四、配置结果

- MCP 名称：`ensp-mcp`（stdio）
- 启动命令：`%USERPROFILE%\.venvs\ensp-mcp\Scripts\python.exe -m ensp_mcp.server`
- 配置位置：Claude Code → `%USERPROFILE%\.claude.json`；WorkBuddy → `%USERPROFILE%\.workbuddy\mcp.json`
- 工具数量：**19 个**（拓扑 9 + 设备库 1 + 启动检查 1 + 设备配置 5 + 命令留痕 1 + 批量并发 1 + 自动验收 1）

---

## 五、手动部署（等价步骤）

```powershell
# 1. Python 3.10+（已装可跳过）
.\python-3.12.10-amd64.exe /quiet InstallAllUsers=0 PrependPath=0

# 2. 建虚拟环境
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv "$env:USERPROFILE\.venvs\ensp-mcp"

# 3. 离线安装（wheel 不匹配就改装源码目录）
& "$env:USERPROFILE\.venvs\ensp-mcp\Scripts\python.exe" -m pip install --no-index --find-links .\wheelhouse ensp-mcp==1.2.1

# 4. Host-Only 网卡（缺了 AR/WLAN 起不来；需管理员）
& "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" hostonlyif create

# 5. 自检
& "$env:USERPROFILE\.venvs\ensp-mcp\Scripts\python.exe" -m ensp_mcp.doctor

# 6. 注册
claude mcp add --scope user ensp-mcp -- "$env:USERPROFILE\.venvs\ensp-mcp\Scripts\python.exe" -m ensp_mcp.server
```

---

## 六、运行前提与已知问题

- 需要 **eNSP 1.3.00**（默认 `C:\Program Files\Huawei\eNSP`）与 **VirtualBox 5.2.x**；
  AR / WLAN / 防火墙等虚拟机设备还依赖对应模板机和镜像。本包**不包含**这些厂商软件。
- **Host-Only 网卡缺失** → AR/WLAN 设备无法启动，脚本会自动创建。
- `USG6000V` 需要额外注册 `vfw_usg.vdi`，`NE40E/CE` 需要 SVRP 镜像；普通实验用 **USG5500** 即可。
- 拓扑类工具是离线文件操作，**不需要 eNSP 运行**；设备类工具要求 eNSP 已启动且设备已开机（图标变绿）。
- 覆盖安装报 **WinError 32 / safe-delete 失败**：说明还有 `ensp-mcp.exe` 在跑。新脚本会自动结束它；
  手动兜底：任务管理器结束 `ensp-mcp.exe`，或 `taskkill /f /im ensp-mcp.exe` 后重跑。
- MCP 服务不会热加载源码：改了 `ensp-mcp-1.3.0\` 里的代码要重新安装并重启客户端。

---

## 七、卸载

```powershell
claude mcp remove --scope user ensp-mcp
Remove-Item -Recurse -Force "$env:USERPROFILE\.venvs\ensp-mcp"
Remove-Item -Recurse -Force "$env:USERPROFILE\.ensp-mcp"   # 命令留痕日志（想留证据就别删）
```

WorkBuddy 侧：删掉 `%USERPROFILE%\.workbuddy\mcp.json` 里的 `ensp-mcp` 条目，或直接在连接器管理里移除。
Python 3.12 如不再需要，可在「设置 → 应用」中卸载。
