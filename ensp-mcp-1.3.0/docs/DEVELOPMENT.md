# 开发和验收

## 安装及运行

Python 3.10 或更新版本。开发依赖沿用运行依赖，测试使用标准库 unittest，不要求 pytest。

```powershell
python -m pip install -e .
python -B -m unittest discover -s tests -t . -v
python -m ensp_mcp.doctor
python -m ensp_mcp.doctor --topo ./work/e2e-lab/acceptance.topo --strict-runtime
```

`python -m ensp_mcp.server` 通过 stdin/stdout 运行 MCP。不要在服务代码中向 stdout 输出日志；日志应写 stderr。修改源码后，需要在客户端重启 MCP 服务进程，旧进程不会自动重新加载。

测试包括离线拓扑功能、原生分区和碰撞检查、UTF-8/UTF-16/CP936 保存保真、
VirtualBox/镜像预检、异常写入、凭据交互、Console 传输失败、配置执行与保存
状态，以及独立 MCP 子进程连接本机 Telnet 测试服务器。集成测试绑定随机
loopback 端口，不需要启动 eNSP。

## 实际 eNSP 验收

先在新目录创建专用测试拓扑，避免使用已有业务实验：

```powershell
python examples/e2e_lab.py create --directory ./work/e2e-lab
```

在 eNSP 中打开生成的 acceptance.topo，启动两台 S5700。该操作需要 eNSP 图形界面和可用的本机设备运行环境。

```powershell
python -m ensp_mcp.doctor --topo ./work/e2e-lab/acceptance.topo --strict-runtime --check-ports
python examples/e2e_lab.py run --directory ./work/e2e-lab --apply
```

验收脚本通过实际 MCP stdio 协议查询版本、下发配置、保存、读回运行和保存配置，再做双向 ping，记录 apply-report.json。默认端口为 2000、2001；如果被已有实验占用，应在专用拓扑中调整 console 端口。

如 console 需要认证，可设置 ENSP_USERNAME、ENSP_PASSWORD；首次改密可设置 ENSP_NEW_PASSWORD。脚本从环境读取密码，不需要把密码写在命令行中。不要将实际密码、运行配置或私人实验日志提交到仓库。

在 eNSP 中手动重启两台设备后，只读复验：

```powershell
python examples/e2e_lab.py run --directory ./work/e2e-lab
```

该命令不重新下发配置，检查读回及双向 ping，记录 verify-report.json。只有这一轮通过，才记录“重启后配置保留”验收成功。

## 结果判断

- 设备工具的 ok 为 false 时，查看 error/code 或逐条 results；命令超时、断线都意味着执行状态未确认，不得自动重放配置命令。
- saveStatus.status 为 saved 才表示本次自动保存识别到成功回显；skipped_due_to_errors 表示没有尝试保存；failed 需检查保存输出。
- device_probe 的 ok 表示命令执行成功，ping 连通性看 parsed.reachable。null 表示回显不足，不能等同于不通或成功。
- topo_validate 检验模型、连接和标注几何；environment_check/doctor 检查已知运行前置条件。两者都不能替代在 eNSP 中真实启动，也无法证明 VLAN、路由或防火墙策略正确。
- 拓扑接口类型校验区分 Ethernet、Serial、POS 家族；GE 的光电介质兼容性仍依赖实际型号、板卡与 eNSP 支持。

## 边界

- 现有 19 个 MCP 工具里，环境与镜像检查部分只读；图形界面开关机、Cloud 网卡绑定、无线关联和设备镜像注册仍需人工完成。
- 普通防火墙实验默认选择 USG5500。USG6000V 只有在显式要求时使用，并在建图后核对 environment_check 的镜像和注册状态。
- eNSP 的 shape/txttip 没有稳定 ID。生成 zones 时，已有标注必须显式 replace_annotations=true 才会清空重建；不能按坐标猜测哪些是自动标注。
- 不保存身份凭据；已有认证会话会复用。要切换凭据，先 device_sessions close，再连接。
- 配置修改没有事务回滚；失败时保留已执行命令结果并停止自动保存。
- `device_batch` 的并发只是把多台设备的会话并行化，同一台设备的命令仍然串行；
  它不提供跨设备原子性，部分设备成功就是部分成功。
- `lab_verify` 只判定命令回显是否符合给定规则，规则来自显式 expect 或内置套件，
  不能证明 eNSP 数据面真的转发、也不能替代抓包与人工复核。
- XML 编辑保留结构、属性和文本，排版统一为 UTF-8、CRLF；不是逐字节保留。
- 多 slot legacy 接口名称采用顺序编号，未穷尽所有扩展板卡；实际 XML 信息会保留，特殊型号应以 eNSP 中的接口名称为准。
- 文件哈希检测能发现读取后到保存前的外部修改；不能在 eNSP 与 MCP 两个独立进程间提供操作系统级事务锁。避免同时在两端保存同一拓扑。

## 打包

```powershell
python -m pip install build
python -m build
```

构建生成 wheel 和源码包。构建前运行全部测试；真实设备验收状态与自动测试状态分别记录。
