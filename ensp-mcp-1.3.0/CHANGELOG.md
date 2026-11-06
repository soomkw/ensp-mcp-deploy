# 更新记录

## 1.3.0 — 2026-11-04

- 新增 `device_batch` MCP 工具：多台设备并发执行命令或下发各自的配置，`concurrency` 默认 4、
  范围 1–16。单台连接失败或命令失败不中断整批，返回每台的 ok、执行条数、错误数、失败命令
  与 `durationMs`；完整回显默认不回传，`include_results=true` 才带上。
- 新增 `lab_verify` MCP 工具：把“配置下发完到底对不对”变成可判定的检查项，返回
  `verdict`（PASS / FAIL / SKIP）和逐条结论。判定规则支持 `contains` / `notContains`、
  `regex` / `notRegex`、`minCount`、`reachable`、`maxLoss`，命令本身报错一律判 FAIL。
- 内置验收套件：`reachable`、`fw_ha`（HRP 角色 + 心跳口 + VRRP 主备）、`bfd`、`ospf`、
  `vrrp`、`vlan`。检查项的 `target` 支持设备名、设备类别和 `all`，按类别自动展开；
  拓扑里没有对应设备时记为 SKIP 而不是 FAIL。
- 批量与验收的命令同样写入命令留痕，同一次调用共用一个 batch id，`lab_verify` 记为独立工具，
  可在 `command_trace` 报告中与配置下发对照。
- 并发在每台设备独立的 console 会话上执行，复用既有的按 `host:port` 连接锁，不会出现两条
  命令抢同一个 console；连接池与空闲回收行为不变。
- 新增 27 个测试（规则判定、套件展开、真实 Telnet 桩上的并发与失败隔离），全套 122 个通过。

## 1.2.1 — 2026-11-04

- 新增 `command_trace` MCP 工具：把每条命令下发到哪台设备整理成可交互的 HTML 可视化报告。
- 所有设备操作（`device_exec`、`device_push_config`、`device_fetch_config`、`device_probe`）自动留痕，
  一条命令一行追加到 `%USERPROFILE%\.ensp-mcp\commands.jsonl`（`ENSP_MCP_LOG_DIR` 可改目录，超过 8 MB 自动轮转）。
- 报告按 `.topo` 的真实坐标绘制拓扑，节点角标显示该设备的命令数与失败数；点节点可跳到对应命令明细，
  每条命令带时间、工具、执行状态和回显（可展开），保存动作 `save` 也单独记录。
- 报告默认落在拓扑同目录的 `<拓扑名>-commands.html`，可直接用浏览器打开；`out_path` 可另存。

## 1.2.0 — 2026-09-09

- 新增 `environment_check` MCP 工具；`doctor` 同步检查 eNSP 安装、VirtualBox 真实版本、硬件虚拟化、Hyper-V/VBS 冲突、Host-Only 网卡、失效磁盘、AR/WLAN 基机、SVRP 和 USG6000V 镜像及注册字段。
- 诊断保持只读，不启动虚拟机、不修改 Windows 功能、不注册镜像；`--strict-runtime` 可让指定拓扑的运行前置条件影响 CLI 退出状态。
- 防火墙默认选择改为 USG5500。`list_models` 返回默认型号和结构化风险；显式 USG6000V 仍按原型号创建，同时提示外部镜像、高启动风险和 USG5500 替代项。
- `topo_layout` 增加 `zones`，用 eNSP 原生矩形和文字字段生成分区背景、颜色和标题；按中英文显示宽度调整设备单元格，自动留出标题栏与分区间距。
- 分区未覆盖的设备默认放入“未分区”。已有手工标注默认不覆盖；普通布局默认拒绝造成标注与设备坐标失步的操作。
- 设备重排会复位已手拖的链路接口标签，使 eNSP 按新端点重新定位；校验改为设备名占位矩形，并增加文字、背景框碰撞及非法标注检查。
- `.topo` 读取新增 GB18030/CP936 回退，兼容 eNSP 官方中文示例；eNSP 200T 安装样例中的 40 份拓扑已全部解析通过。
- PNG 渲染保留为可选辅助工具，从主验收流程移除；示例建图不再自动生成预览图。

兼容性验证覆盖 VirtualBox 5.2.x、模板机、Host-Only 网卡、Hyper-V/VBS raw-mode 冲突、缺失的 `vfw_usg.vdi`/`svrpbox.img`，以及 USG5500 本地模拟器文件。诊断只报告检测结果，不修改系统配置。

## 1.1.0 — 2026-09-08

- 修复 Console 超时和 EOF 被当作成功的问题，返回错误代码及部分回显，关闭异常连接，不自动重放命令。
- 批量执行默认遇错停止；保留已执行结果；失败后不保存，保存失败单独返回状态。
- 设备工具支持 username/password/new_password，处理账号密码登录、仅密码登录和首次设置/修改密码。
- 修复分页重复应答、带连字符的主机名、确认提示符误识别；补齐保存文件名确认及 CE 未提交配置处理。
- 连接池定期回收闲置连接，关闭会话等待正在执行的操作；MCP 退出时释放全部连接。
- 拓扑编辑保留图形、文字标注、XML 文本、注释、扩展属性、实际 slot 信息和同一 line 下的多条接口连接。
- 拓扑及文本导出采用原子写入；检测读取后文件被其他程序修改；本进程内串行化拓扑操作。
- 查询和连线使用已加载 XML 的实际接口。型号核对增加接口编号、类型和同型号不同配置的比较。
- 补齐接口与线型兼容性、设备 ID、console 范围、Cloud 接口、坐标、设备数量、终端参数等校验。
- 修复 draw.io 双引号和换行转义、Mermaid 特殊字符及保留名称；导出自动创建父目录。
- 配置下发支持 # 分段；配置差异保留顺序和重复行，提供 unifiedDiff；无法解析 ping 时 reachable 返回 null。
- 新增环境诊断 CLI、两交换机 eNSP 验收脚本、单元和真实 stdio/Telnet 协议集成测试。

行为变化：调用方应检查 ok、results 和 saveStatus。stop_on_error 默认 true；若需要继续执行后续命令，可显式传 false，但出错后仍不会执行自动保存。真实设备已生效的配置不会自动回滚。

本版本自动测试使用协议测试桩；其成功不代表全部 Huawei 设备型号、VRP 版本和首次改密提示形式都已在 eNSP 中验证。
