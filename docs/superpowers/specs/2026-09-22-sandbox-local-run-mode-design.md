# 沙箱 / 本地电脑运行模式

## Goal

Composer 底部访问状态改为双模式：**沙箱运行** / **本地电脑运行**。本地模式通过输入框旁「+」调起系统文件夹对话框选目录，热重载工作区、文件/终端、本机系统工具与知识库索引绑定；权限仍走四档 `exec_mode`。

## Behavior

| 模式 | 工作区 | local_access / system | 「+」 |
|------|--------|------------------------|------|
| sandbox | `config.yaml` 默认 `workspace_root` | 关闭 | 提示先切本地 |
| local（未选目录） | 仍为默认沙箱根 | 关闭 | 系统选文件夹 → 添加项目 |
| local（已选目录） | 激活项目路径 | 开启（roots=项目列表） | 可再添加/切换项目 |

权限：既有 plan / confirm_writes / auto_workspace / full_access，闸门与边界不变。

## API

- `PUT /api/runtime/run-mode` `{ "mode": "sandbox"|"local" }`
- `POST /api/runtime/pick-folder` → `{ path }` 或 `{ cancelled: true }`（本机 tkinter 对话框）
- 现有 `POST /api/runtime/projects` 在添加时将 `run_mode` 设为 `local`

## Chain

`reconfigure_runtime` 已重绑 workspace 工具、broker、indexer；前端 `loadRuntime` + `refreshChainStatus` + ChainModules 刷新。
