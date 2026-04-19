# AstrBot 远程知识库插件

> **作者**: elecvoid243
> **版本**: 1.1.0
> **创建时间**: 2026-04-15

## 概述

AstrBot 远程知识库插件（`astrbot_plugin_remote_kb`）将 AstrBot 的知识库查询功能包装为 HTTP 服务，支持跨 AstrBot 实例的知识库共享与查询。

该插件同时支持**服务端模式**、**客户端模式**，实现多维度的远程知识库访问。

## 功能特性

### 服务端模式 (Server Mode)

- 🚀 启动内置 HTTP 服务器
- 📚 暴露知识库列表 API
- 🔍 提供知识库检索 API
- 📤 支持文档上传 API
- 🔒 API Key 认证保护
- 🌐 CORS 跨域支持
- ❤️ 健康检查端点
- 🎯 支持配置允许暴露的知识库列表

### 客户端模式 (Client Mode)

- 🌐 向远程 AstrBot 实例查询知识库
- 🔗 支持配置多个远程服务器
- 🔑 支持 API Key 认证
- 📋 获取远程知识库列表
- 💬 聊天指令接口

### Agent工具

- 🤖 为 Agent 自动注册远程知识库查询工具
- 🔍 `astrbot_remote_kb_search` - 查询远程知识库
- 📋 `get_remote_kb_servers` - 获取可用服务器及知识库列表
- 📝 动态生成工具描述，包含可用的远程服务器信息
- 💡 智能提示 Agent 选择正确的服务器

## 安装

1. 将插件文件夹 `astrbot_plugin_remote_kb` 复制到 AstrBot 的 `plugins` 目录下
2. 在 AstrBot WebUI 中启用插件并配置

## 配置说明

插件通过 WebUI 配置界面管理，主要分为两部分：

### 服务端设置 (`server_settings`)

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | `true` | 启用知识库服务端 |
| `host` | string | `0.0.0.0` | HTTP服务监听地址 |
| `port` | int | `8550` | HTTP服务监听端口 |
| `api_key` | string | `""` | API认证密钥（为空则不启用认证） |
| `cors_enabled` | bool | `true` | 启用CORS支持 |
| `allowed_kb_names` | list | `[]` | 允许暴露的知识库（留空则暴露所有） |

### 客户端设置 (`client_settings`)

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | `false` | 启用知识库客户端 |
| `remote_servers` | list | `[]` | 远程服务器配置列表 |
| `default_top_k` | int | `5` | 默认检索返回数 |
| `default_top_m` | int | `3` | 最终返回结果数 |

### 远程服务器配置

在客户端设置中添加远程服务器，每个服务器包含：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `name` | string | 服务器名称/别名 |
| `base_url` | string | 服务器地址（格式: http://host:port） |
| `api_key` | string | 服务器API密钥 |

### WebUI 配置示例

```
服务端设置:
├── enabled: ✓
├── host: 0.0.0.0
├── port: 8550
├── api_key: my-secret-key
├── cors_enabled: ✓
└── allowed_kb_names: []  (留空暴露所有)

客户端设置:
├── enabled: ✓
├── remote_servers:
│   └── + 添加远程服务器
│       ├── name: server_a
│       ├── base_url: http://192.168.1.10:8550
│       └── api_key: remote-key
├── default_top_k: 5
└── default_top_m: 3
```

## API 接口文档

### 服务端 API

#### 1. 健康检查

```
GET /health
```

**响应示例:**
```json
{
  "status": "healthy",
  "knowledge_base": "available"
}
```

#### 2. 列出知识库

```
GET /api/kbs
```

**响应示例:**
```json
{
  "knowledge_bases": [
    {
      "kb_id": "uuid-xxx",
      "kb_name": "文档库",
      "description": "项目文档集合",
      "emoji": "📚",
      "doc_count": 10,
      "chunk_count": 256
    }
  ]
}
```

> 注意：如果配置了 `allowed_kb_names`，只返回允许暴露的知识库。

#### 3. 获取知识库详情

```
GET /api/kb/{kb_name}
```

**响应示例:**
```json
{
  "kb_id": "uuid-xxx",
  "kb_name": "文档库",
  "documents": [
    {
      "doc_id": "doc-uuid",
      "doc_name": "使用指南.pdf",
      "file_type": "pdf",
      "chunk_count": 25
    }
  ]
}
```

#### 4. 知识库检索 (核心接口)

```
POST /api/retrieve
Content-Type: application/json
Authorization: Bearer <api_key>  (可选)
```

**请求体:**
```json
{
  "query": "如何配置AstrBot?",
  "kb_names": ["文档库", "FAQ"],
  "top_k_fusion": 20,
  "top_m_final": 5
}
```

**响应示例:**
```json
{
  "context_text": "AstrBot配置方法如下:\n1. 首先安装...\n2. 然后配置...",
  "results": [
    {
      "chunk_id": "chunk-uuid",
      "doc_id": "doc-uuid",
      "doc_name": "使用指南.pdf",
      "kb_name": "文档库",
      "content": "AstrBot配置方法如下:\n1. 首先安装...",
      "score": 0.95
    }
  ]
}
```

#### 5. 上传文档

```
POST /api/kb/{kb_name}/upload
Content-Type: multipart/form-data
```

或:

```
POST /api/kb/{kb_name}/upload
Content-Type: application/json
```

**JSON格式请求体:**
```json
{
  "file_name": "document.pdf",
  "content": "base64编码的文件内容"
}
```

## 客户端使用示例

### Python 示例

```python
import aiohttp
import json

async def query_remote_kb():
    base_url = "http://192.168.1.100:8550"
    api_key = "your-secret-key"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    payload = {
        "query": "AstrBot是什么?",
        "top_k_fusion": 10,
        "top_m_final": 3
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/api/retrieve",
            json=payload,
            headers=headers
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                print(result["context_text"])
            else:
                print(f"Error: {resp.status}")

import asyncio
asyncio.run(query_remote_kb())
```

### cURL 示例

```bash
# 健康检查
curl http://localhost:8550/health

# 列出知识库
curl http://localhost:8550/api/kbs

# 检索
curl -X POST http://localhost:8550/api/retrieve \
  -H "Content-Type: application/json" \
  -d '{"query": "AstrBot是什么?", "top_k_fusion": 10, "top_m_final": 3}'

# 带认证的检索
curl -X POST http://localhost:8550/api/retrieve \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-key" \
  -d '{"query": "AstrBot是什么?"}'
```

## Agent工具使用

当插件启用客户端模式并配置了远程服务器后，Agent 将自动获得两个工具：

### 1. `get_remote_kb_servers`

获取已配置的远程知识库服务器列表。

**使用场景**: Agent 需要了解有哪些远程服务器可用时调用。

**返回信息**:
- 服务器名称
- 服务器地址
- 远程服务器提供的知识库列表（实时获取）

**返回示例**:
```
已配置的远程知识库服务器:

1. server_a
   地址: http://192.168.1.10:8550
   知识库: 文档库, FAQ, 技术手册

2. server_b
   地址: http://192.168.1.11:8550
   知识库: (无法获取)
```

### 2. `astrbot_remote_kb_search`

查询远程知识库获取相关信息。

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `server_name` | string | ✅ | 要查询的远程服务器名称 |
| `query` | string | ✅ | 查询关键词 |
| `kb_names` | array[string] | ❌ | 逗号分隔的知识库名称 |

**使用示例**:

Agent 可以这样调用：
```json
{
  "server_name": "server_a",
  "query": "AstrBot的配置方法",
  "kb_names": ["文档库, FAQ"]
}
```

### 工作流程

当用户提问涉及远程知识库内容时，Agent 会：

1. **识别需求**: 判断用户问题是否需要查询远程知识库
2. **获取列表**: 调用 `get_remote_kb_servers` 确认可用服务器
3. **执行查询**: 调用 `astrbot_remote_kb_search` 查询远程知识库
4. **整合结果**: 将检索结果整合到回答中

## 聊天指令

插件提供以下聊天指令（仅用于测试）:

| 指令 | 说明 | 用法 |
|------|------|------|
| `/remote_kb_list` | 列出已配置的远程服务器 | 直接发送即可 |
| `/remote_kb_query <服务器名> <查询>` | 查询远程知识库 | `/remote_kb_query server_a 什么是RAG` |
| `/kb_servers_status` | 检查远程服务器状态 | 直接发送即可 |

## 使用场景

### 场景一: 知识库共享中心

```
┌─────────────────┐         ┌─────────────────┐
│   AstrBot A      │         │   AstrBot B      │
│   (服务端)        │◄───────►│   (客户端)       │
│   端口: 8550     │   HTTP  │                 │
└─────────────────┘         └─────────────────┘
```

A 实例运行服务端，暴露知识库 API；B 实例通过客户端模式查询 A 的知识库。

### 场景二: 多机器人协作

```
┌─────────────────┐
│   AstrBot Hub    │
│  (中央协调器)     │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌───────┐ ┌───────┐
│Bot A  │ │Bot B  │
│KB:技术│ │KB:运营│
└───────┘ └───────┘
```

中央 AstrBot 作为协调器，可以同时查询多个 Bot 的知识库。

## 安全建议

1. **启用 API Key 认证**: 在生产环境中务必设置 `api_key`
2. **限制监听地址**: 如仅本地使用，设置 `host` 为 `127.0.0.1`
3. **配置允许列表**: 设置 `allowed_kb_names` 只暴露必要的知识库
4. **防火墙保护**: 对外暴露时使用防火墙限制访问 IP
5. **HTTPS**: 生产环境建议使用 Nginx/Caddy 反向代理并启用 HTTPS

## 故障排除

### Q: 插件启动后无法访问 API?

- 检查 `port` 是否被占用
- 确认防火墙允许该端口访问
- 查看 AstrBot 日志中的错误信息

### Q: 检索返回空结果?

- 确认知识库中已有文档
- 检查 `kb_names` 是否正确（区分大小写）
- 尝试增大 `top_k_fusion` 参数

### Q: 认证失败?

- 确认客户端和服务端的 `api_key` 一致
- 检查请求头格式: `Authorization: Bearer <key>`

### Q: 服务端知识库未全部暴露?

- 检查 `allowed_kb_names` 配置是否为空（留空表示暴露所有）
- 确认知识库名称与 WebUI 中显示的名称完全一致

## 更新日志

### v1.1.0

- `get_remote_kb_servers` 工具支持实时获取远程服务器的知识库列表
- 简化配置结构为 `server_settings` 和 `client_settings`
- 支持配置服务端允许暴露的知识库列表

- 新增 `astrbot_remote_kb_search` 工具，允许 Agent 直接查询远程知识库
- 新增 `get_remote_kb_servers` 工具，获取可用服务器列表
- 动态生成工具描述，向 Agent 展示可用的远程服务器和知识库信息

### v1.0.0

- 初始版本
- 支持服务端模式和客户端模式
- 提供检索、上传、列表等核心 API
- 支持 API Key 认证和 CORS

## 许可证

本插件遵循 AstrBot 插件协议。
