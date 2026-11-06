# 参与贡献

感谢你改进 eNSP MCP。提交代码前请先搜索现有 Issue，说明使用的 eNSP、
VirtualBox、Windows 和 Python 版本，以及能够复现问题的最小拓扑或调用参数。
请勿提交设备密码、运行配置、私有拓扑、厂商镜像或安装包。

## 开发环境

```powershell
git clone https://github.com/Heaxu/ensp-mcp.git
cd ensp-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

## 验证

```powershell
python -B -m unittest discover -s tests -t . -v
python -B -m compileall -q src tests
```

涉及 `.topo` 读写、设备目录或分区标注的改动，需要添加针对真实 eNSP XML
结构的回归测试。涉及启动兼容性的改动应保持诊断只读，不应自动启动虚拟机、
修改 Windows 功能、注册镜像或删除 VirtualBox 磁盘。

## 提交说明

- 每个提交聚焦一个问题，并写清行为变化。
- 新功能同步更新 README、CHANGELOG 和测试。
- 保持兼容 Python 3.10 及以上版本。
- Pull Request 中列出运行过的验证命令和仍需真实 eNSP 验证的限制。
