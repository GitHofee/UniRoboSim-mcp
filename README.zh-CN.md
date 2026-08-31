# UniRoboSim MCP

[English](README.md) | [简体中文](README.zh-CN.md)

`unirobosim-mcp` 0.10 通过 MCP 向兼容 Client 提供 UniRoboSim 证据、仿真状态、后端相机图像和显式启用的仿真控制能力。Server 提供两种部署 Profile：

- **Evidence Profile（默认）：** 对运维方指定 Evidence Root 的有边界只读访问。
- **Control Profile（显式启用）：** 在 Evidence Tool 基础上，增加对本 Server 创建并持有的仿真会话的 Read 与 Control Tool。

Server 不会接管其他应用创建的会话。

## 安装

支持 Python `>=3.11,<3.13`。0.10.0 版本要求 UniRoboSim Core
`>=0.10,<0.11`。Core、本包以及目标后端的 Adapter 应安装在同一环境中。

```bash
conda create -n unirobosim-mcp python=3.12 pip -y
conda activate unirobosim-mcp

git clone https://github.com/GitHofee/UniRoboSim.git
git clone https://github.com/GitHofee/UniRoboSim-mcp.git
git clone https://github.com/GitHofee/UniRoboSim-mujoco.git  # 后端示例

python -m pip install ./UniRoboSim ./UniRoboSim-mcp ./UniRoboSim-mujoco
python -m pip check
```

0.10.0 发布门在 Core 0.10.0 上执行 Evidence、持有会话的控制、
相机编码和真实 MCP Client 测试。MCP 不引入对 FastSim 或仿真器
Adapter 的依赖；它们仍是由应用选择的可选同级包。紧凑相机帧
继续使用 Core 的 `ArrayValue.to_bytes()` API，并兼容 Core 0.10 的紧凑数组表示。

常规部署使用当前 MCP 2.x 运行时。Isaac Lab 3.0 环境需要保留已验证的 Pydantic 与
Uvicorn 版本约束，因此应安装兼容性扩展：

```bash
python -m pip install './UniRoboSim-mcp[isaaclab]'
```

该扩展选择 MCP `1.10.1`，对外提供相同的 UniRoboSim 工具目录，并已通过 Isaac Sim
6.0.1 的真实 stdio 协议验收。

## Evidence Profile

```bash
unirobosim-mcp --root /absolute/path/to/approved/evidence
```

也可通过 `UNIROBOSIM_EVIDENCE_ROOT` 配置 Root：

```bash
export UNIROBOSIM_EVIDENCE_ROOT=/absolute/path/to/approved/evidence
unirobosim-mcp
```

| Tool | 合同 |
| --- | --- |
| `evidence_server_info` | 返回当前 Root、硬查询限制和控制状态。 |
| `list_debug_evidence` | 使用有界 POSIX glob 列出允许的证据。 |
| `read_debug_evidence` | 读取一个有边界的 UTF-8 或 JSON 产物。 |
| `summarize_debug_trace` | 验证已关闭 Trace 并返回紧凑 Manifest。 |
| `query_debug_events` | 查询 publish、clear、reset 事件，不返回完整几何。 |
| `query_debug_reports` | 查询 accepted、filtered 和 dropped 发布决策。 |
| `query_debug_primitives` | 重建指定 sequence 的活动 Debug 图元。 |

绝对路径、目录穿越、逃逸 Symlink、未批准扩展名、超大文件、过量扫描和过量结果都会被拒绝。

## Control Profile

Control 必须显式启用。本地资产所在目录没有通过 `--asset-root` 加入白名单时，资产加载请求会被拒绝。

```bash
unirobosim-mcp \
  --root /absolute/path/to/approved/evidence \
  --enable-control \
  --asset-root /absolute/path/to/approved/assets \
  --max-sessions 2 \
  --lease-timeout-seconds 300
```

### Read API

Read Tool 需要 Session ID，但不要求写租约。

| Tool | 合同 |
| --- | --- |
| `simulation_list_backends` | 发现并探测已安装的 Backend Entry Point。 |
| `simulation_list_sessions` | 仅列出本 Server 持有的会话，不返回租约值。 |
| `simulation_scene_snapshot` | 返回用于对象和相机发现的可移植场景图。 |
| `simulation_get_entity` | 读取刚体、铰接体、柔性体、粒子流体或相机的类型化状态。 |
| `simulation_capture_camera` | 返回由后端 RGB 相机缓冲区编码得到的 MCP PNG Image。 |

`simulation_get_entity` 返回规范路径、实体类型、原始 MCP 配置、仿真 Tick、数组 Shape/Dtype 和类型专用数据。`include_values=true` 返回有界数值；`include_contact=true` 为刚体增加接触状态。

`simulation_capture_camera` 不是桌面或浏览器截图。它通过所选 Backend 调用 `Camera.read("rgb")`，验证规范 `[environment,height,width,3]` uint8 缓冲区，再将该缓冲区编码为 PNG。设置 `save_to_evidence=true` 后，图像同时写入 `<root>/screenshots/`，返回 SHA-256 和分辨率。

### Control API

所有写操作都需要 `simulation_create` 返回的不透明 `lease_id`，以及唯一 `command_id`。

| Tool | 合同 |
| --- | --- |
| `simulation_control_info` | 返回所有权策略、资产白名单和硬资源限制。 |
| `simulation_create` | 为显式指定的 Backend 创建由 Server 持有的 EasyAPI 会话。 |
| `simulation_configure_entity` | 启动前添加 Box、刚体资产、铰接体、相机、柔性体或粒子流体。 |
| `simulation_start` | 编译场景并返回 Backend Build Fingerprint。 |
| `simulation_renew_lease` | 延长写租约，不更换租约值。 |
| `simulation_step` | 按有界步数推进仿真。 |
| `simulation_reset` | 重置全部或指定 Environment。 |
| `simulation_command` | 提交铰接、刚体 Wrench、柔性体、流体、场景或 Debug Clear 命令。 |
| `simulation_close` | 关闭 Server 持有的会话并释放 Backend 资源。 |

使用相同输入重复提交 `command_id` 时，Server 返回缓存结果并设置 `idempotent_replay=true`；同一标识对应不同输入时会被拒绝。租约过期的会话自动关闭。所有已应用或被拒绝的写操作都记录到 `mcp-control-audit.jsonl`，审计记录不包含租约值。

## Agent 操作规则

Agent 使用 Control Profile 时必须遵循以下顺序：

1. 调用 `simulation_list_backends`，显式选择可用 Backend。
2. 调用 `simulation_create`；返回的租约只用于写操作。
3. 使用唯一 Command ID 添加全部实体，然后调用 `simulation_start`。
4. 使用 `simulation_scene_snapshot` 获取规范对象和相机路径。
5. 使用 `simulation_get_entity` 做定向状态读取，使用 `simulation_capture_camera` 做视觉验证。
6. 只有重试完全相同的写请求时才能复用 Command ID。
7. 无论流程成功或失败，都必须为每个已创建会话调用 `simulation_close`。

Agent 不能根据 Tool 是否存在推断 Backend 能力；不支持的能力由 Capability Negotiation 或目标 Adapter 明确报告。

## 本机 HTTP

```bash
unirobosim-mcp \
  --root /absolute/path/to/approved/evidence \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8766
```

未认证 HTTP 仅允许 `127.0.0.1`、`localhost` 或 `::1`。远程部署必须增加具有认证和授权能力的 Gateway。Control Profile 不能直接暴露在不受信任网络中。

## 编程嵌入

```python
from pathlib import Path

from unirobosim_mcp import ControlLimits, EvidenceLimits, SimulationControl, create_server

root = Path("/approved/evidence")
control = SimulationControl(
    root,
    asset_roots=(Path("/approved/assets"),),
    limits=ControlLimits(max_sessions=1, lease_timeout_seconds=120),
)
server = create_server(
    root,
    limits=EvidenceLimits(max_results=50, max_query_items=100),
    control=control,
)
server.run(transport="stdio")
```

## 验证

```bash
python -m pip install -e '.[dev]'
ruff format --check src tests
ruff check src tests
mypy src
coverage run -m pytest
coverage report
```

发布验收通过真实 In-process MCP Client 调用每一个公开 MCP Tool。补充合同测试覆盖全部支持的实体类型和命令族、租约、幂等、过期、资产白名单、资源限制、审计记录、PNG 编码与落盘截图。每个已安装仿真器 Adapter 还要单独执行原生验收；某项功能只有原生运行成功后才能标记为该 Backend 通过。

Core 合同与 Adapter 安装见 [UniRoboSim Core](https://github.com/GitHofee/UniRoboSim.git)。
