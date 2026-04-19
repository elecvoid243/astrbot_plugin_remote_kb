"""
AstrBot 远程知识库插件

将Astrbot的知识库查询功能包装为HTTP服务，支持跨Astrbot实例的知识库共享与查询。

功能说明:
- 服务端模式: 将本地知识库暴露为HTTP API，供其他Astrbot实例查询
- 客户端模式: 向远程Astrbot实例发起知识库查询请求
- Agent工具: 为Agent提供远程知识库查询能力

Author: elecvoid243
Created: 2026-04-15
"""

import asyncio
import base64
import binascii
import json
import traceback
from typing import Any, Optional

import aiohttp
from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig


# ==================== 插件主体 ====================

@register("astrbot_plugin_remote_kb", "elecvoid243",
           "将Astrbot知识库查询功能包装为HTTP服务，支持跨Astrbot实例的知识库共享与查询。服务端和客户端都需要安装",
           "1.1.0")
class RemoteKBSPlugin(Star):
    """
    远程知识库插件

    提供三种工作模式:
    1. Server Mode (服务端模式): 启动HTTP服务器，提供知识库查询API
    2. Client Mode (客户端模式): 向远程Astrbot实例查询知识库
    3. Agent工具模式: 为Agent提供远程知识库查询能力 (astrbot_remote_kb_search)
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # HTTP服务相关
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

        # 知识库管理器
        self.kb_manager = None

        # 客户端会话
        self._client_session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        """插件初始化"""
        logger.info("RemoteKB Plugin: 正在初始化...")

        # 获取知识库管理器
        self.kb_manager = self.context.kb_manager

        # 读取服务端配置
        server_settings = self.config.get("server_settings", {})
        if isinstance(server_settings, dict) and server_settings.get("enabled", True):
            asyncio.create_task(self._start_server())
            host = server_settings.get("host", "0.0.0.0")
            port = server_settings.get("port", 8550)
            logger.info(f"RemoteKB Plugin: 服务端模式已启用，将监听 {host}:{port}")

        # 读取客户端配置
        client_settings = self.config.get("client_settings", {})
        if isinstance(client_settings, dict) and client_settings.get("enabled", False):
            logger.info("RemoteKB Plugin: 客户端模式已启用")
            self._client_session = aiohttp.ClientSession()

        logger.info("RemoteKB Plugin: 初始化完成")

    async def terminate(self) -> None:
        """插件卸载/停用时的清理"""
        logger.info("RemoteKB Plugin: 正在停止...")

        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        if self.app:
            self.app = None

        if self._client_session:
            await self._client_session.close()

        logger.info("RemoteKB Plugin: 已停止")

    # ==================== 配置提取辅助方法 ====================

    def _get_default_retrieve_params(self) -> tuple[int, int]:
        """获取检索默认参数"""
        client_settings = self.config.get("client_settings", {})
        if isinstance(client_settings, dict):
            return (
                client_settings.get("default_top_k", 5),
                client_settings.get("default_top_m", 3)
            )
        return (5, 3)

    def _get_server_config(self, server_name: str) -> Optional[dict]:
        """根据服务器名称获取服务器配置"""
        servers = self._get_remote_servers()
        for s in servers:
            if s.get("name") == server_name:
                return s
        return None

    # ==================== HTTP服务端实现 ====================

    async def _start_server(self) -> None:
        """启动HTTP服务器"""
        server_settings = self.config.get("server_settings", {})
        if not isinstance(server_settings, dict):
            server_settings = {}

        host = server_settings.get("host", "0.0.0.0")
        port = server_settings.get("port", 8550)
        api_key = server_settings.get("api_key", "")
        cors_enabled = server_settings.get("cors_enabled", True)
        allowed_kb_names = server_settings.get("allowed_kb_names", [])

        # 确保 allowed_kb_names 是列表
        if not isinstance(allowed_kb_names, list):
            allowed_kb_names = []

        logger.info(f"[RemoteKB] Server config loaded - allowed_kb_names: {allowed_kb_names}")

        self.app = web.Application(
            middlewares=[self._auth_middleware]
        )

        # 保存配置供中间件使用
        self.app["api_key"] = api_key
        self.app["cors_enabled"] = cors_enabled
        self.app["allowed_kb_names"] = allowed_kb_names

        # 注册路由
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/health", self._handle_health)
        self.app.router.add_get("/api/kbs", self._handle_list_kbs)
        self.app.router.add_post("/api/retrieve", self._handle_retrieve)
        self.app.router.add_get("/api/kb/{kb_name}", self._handle_get_kb_info)
        self.app.router.add_post("/api/kb/{kb_name}/upload", self._handle_upload_document)

        # 注册 CORS 预检请求处理器
        self.app.router.add_options("/api/retrieve", self._handle_cors_preflight)
        self.app.router.add_options("/api/kbs", self._handle_cors_preflight)
        self.app.router.add_options("/api/kb/{kb_name}", self._handle_cors_preflight)
        self.app.router.add_options("/api/kb/{kb_name}/upload", self._handle_cors_preflight)

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()

        logger.info(f"RemoteKB HTTP Server started at http://{host}:{port}")

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler) -> web.Response:
        """认证中间件"""
        api_key = ""
        if self.app and "api_key" in self.app:
            api_key = self.app["api_key"]

        if not api_key:
            return await handler(request)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token == api_key:
                return await handler(request)

        if request.method == "OPTIONS":
            return await handler(request)

        return web.json_response(
            {"error": "Unauthorized", "message": "Invalid or missing API key"},
            status=401
        )

    def _add_cors_headers(self, response: web.Response) -> web.Response:
        """添加CORS头"""
        if self.app and self.app.get("cors_enabled", True):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    async def _handle_cors_preflight(self, request: web.Request) -> web.Response:
        """处理 CORS 预检请求"""
        response = web.Response()
        return self._add_cors_headers(response)

    async def _handle_index(self, request: web.Request) -> web.Response:
        """首页处理"""
        response = web.json_response({
            "service": "AstrBot Remote KB Server",
            "version": "1.2.0",
            "endpoints": {
                "health": "/health",
                "list_kbs": "/api/kbs",
                "retrieve": "/api/retrieve",
                "kb_info": "/api/kb/{kb_name}",
                "upload": "/api/kb/{kb_name}/upload"
            }
        })
        return self._add_cors_headers(response)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """健康检查"""
        kb_status = "available" if self.kb_manager else "unavailable"
        response = web.json_response({
            "status": "healthy",
            "knowledge_base": kb_status
        })
        return self._add_cors_headers(response)

    async def _handle_list_kbs(self, request: web.Request) -> web.Response:
        """列出所有知识库"""
        try:
            if not self.kb_manager:
                return web.json_response(
                    {"error": "Knowledge base not available"},
                    status=503
                )

            kbs = await self.kb_manager.list_kbs()
            allowed_kb_names = self.app.get("allowed_kb_names", []) if self.app else []

            # 确保 allowed_kb_names 是列表且不为空
            if not isinstance(allowed_kb_names, list):
                allowed_kb_names = []

            kb_list = []
            for kb in kbs:
                # 如果配置了 allowed_kb_names 且不为空，则过滤
                if allowed_kb_names and kb.kb_name not in allowed_kb_names:
                    continue
                kb_list.append({
                    "kb_id": kb.kb_id,
                    "kb_name": kb.kb_name,
                    "description": kb.description,
                    "emoji": getattr(kb, 'emoji', '📚'),
                    "doc_count": getattr(kb, 'doc_count', 0),
                    "chunk_count": getattr(kb, 'chunk_count', 0)
                })

            response = web.json_response({"knowledge_bases": kb_list})
            return self._add_cors_headers(response)

        except Exception as e:
            logger.error(f"Error listing KBs: {e}\n{traceback.format_exc()}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_kb_info(self, request: web.Request) -> web.Response:
        """获取指定知识库的详细信息"""
        try:
            kb_name = request.match_info["kb_name"]

            if not self.kb_manager:
                return web.json_response(
                    {"error": "Knowledge base not available"},
                    status=503
                )

            kb_helper = await self.kb_manager.get_kb_by_name(kb_name)
            if not kb_helper:
                return web.json_response(
                    {"error": f"Knowledge base '{kb_name}' not found"},
                    status=404
                )

            kb = kb_helper.kb
            docs = await kb_helper.list_documents(limit=100)
            doc_list = []
            for doc in docs:
                doc_list.append({
                    "doc_id": doc.doc_id,
                    "doc_name": doc.doc_name,
                    "file_type": doc.file_type,
                    "chunk_count": doc.chunk_count,
                    "created_at": str(doc.created_at) if doc.created_at else None
                })

            response = web.json_response({
                "kb_id": kb.kb_id,
                "kb_name": kb.kb_name,
                "description": kb.description,
                "emoji": getattr(kb, 'emoji', '📚'),
                "doc_count": len(doc_list),
                "documents": doc_list
            })
            return self._add_cors_headers(response)

        except Exception as e:
            logger.error(f"Error getting KB info: {e}\n{traceback.format_exc()}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_retrieve(self, request: web.Request) -> web.Response:
        """知识库检索API"""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        query = data.get("query", "")
        if not query:
            return web.json_response({"error": "Missing required field: query"}, status=400)

        # 使用辅助方法获取默认参数
        default_top_k, default_top_m = self._get_default_retrieve_params()

        kb_names = data.get("kb_names", [])
        top_k_fusion = data.get("top_k_fusion", default_top_k)
        top_m_final = data.get("top_m_final", default_top_m)

        # 获取允许的知识库列表
        allowed_kb_names = self.app.get("allowed_kb_names", []) if self.app else []
        # 确保 allowed_kb_names 是列表且不为空
        if not isinstance(allowed_kb_names, list):
            allowed_kb_names = []

        # 如果配置了 allowed_kb_names 且不为空，则应用过滤
        if allowed_kb_names:
            if kb_names:
                kb_names = [kb for kb in kb_names if kb in allowed_kb_names]
            else:
                kb_names = allowed_kb_names

        try:
            if not self.kb_manager:
                return web.json_response(
                    {"error": "Knowledge base not available"},
                    status=503
                )

            result = await self.kb_manager.retrieve(
                query=query,
                kb_names=kb_names,
                top_k_fusion=top_k_fusion,
                top_m_final=top_m_final
            )

            if not result:
                result = {"context_text": "", "results": []}

            response = web.json_response(result)
            return self._add_cors_headers(response)

        except Exception as e:
            logger.error(f"Error retrieving: {e}\n{traceback.format_exc()}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_upload_document(self, request: web.Request) -> web.Response:
        """上传文档到知识库API"""
        kb_name = request.match_info["kb_name"]

        try:
            content_type = request.headers.get("Content-Type", "")

            if "multipart/form-data" in content_type:
                reader = await request.multipart()
                file_name = "uploaded_file"
                file_content = b""

                async for part in reader:
                    if part.name == "file":
                        file_name = part.filename or file_name
                        file_content = await part.read()
                    elif part.name == "kb_name":
                        kb_name = (await part.text()).strip() or kb_name

                if not file_content:
                    return web.json_response(
                        {"error": "No file content received"},
                        status=400
                    )

                doc = await self._upload_to_kb(kb_name, file_name, file_content)
                response = web.json_response({
                    "success": True,
                    "document": {
                        "doc_id": doc.doc_id,
                        "doc_name": doc.doc_name,
                        "chunk_count": doc.chunk_count
                    }
                })
                return self._add_cors_headers(response)

            else:
                data = await request.json()
                file_name = data.get("file_name", "document.txt")
                file_content_b64 = data.get("content")

                if not file_content_b64:
                    return web.json_response(
                        {"error": "Missing required field: content (base64 encoded)"},
                        status=400
                    )

                # 添加 Base64 解码异常处理
                try:
                    file_content = base64.b64decode(file_content_b64)
                except binascii.Error:
                    return web.json_response(
                        {"error": "Invalid Base64 content"},
                        status=400
                    )

                doc = await self._upload_to_kb(kb_name, file_name, file_content)
                response = web.json_response({
                    "success": True,
                    "document": {
                        "doc_id": doc.doc_id,
                        "doc_name": doc.doc_name,
                        "chunk_count": doc.chunk_count
                    }
                })
                return self._add_cors_headers(response)

        except Exception as e:
            logger.error(f"Error uploading document: {e}\n{traceback.format_exc()}")
            return web.json_response({"error": str(e)}, status=500)

    async def _upload_to_kb(self, kb_name: str, file_name: str,
                           file_content: bytes) -> Any:
        """上传文档到指定知识库"""
        kb_helper = await self.kb_manager.get_kb_by_name(kb_name)
        if not kb_helper:
            raise ValueError(f"Knowledge base '{kb_name}' not found")

        file_ext = file_name.split(".")[-1].lower() if "." in file_name else "txt"

        doc = await kb_helper.upload_document(
            file_name=file_name,
            file_content=file_content,
            file_type=file_ext,
            chunk_size=512,
            chunk_overlap=50,
            batch_size=32
        )

        return doc

    # ==================== 客户端模式实现 ====================

    def _get_remote_servers(self) -> list:
        """获取远程服务器配置"""
        client_settings = self.config.get("client_settings", {})
        if isinstance(client_settings, dict):
            return client_settings.get("remote_servers", [])
        return []

    async def _ensure_client_session(self) -> aiohttp.ClientSession:
        """确保客户端会话已创建"""
        if not self._client_session:
            self._client_session = aiohttp.ClientSession()
        return self._client_session

    async def _query_remote_server(
        self,
        server_name: str,
        query: str,
        kb_names: Optional[list[str]] = None,
        top_k_fusion: int = 5,
        top_m_final: int = 3
    ) -> Optional[dict]:
        """向远程知识库服务器发起查询"""
        session = await self._ensure_client_session()

        server_config = self._get_server_config(server_name)
        if not server_config:
            logger.error(f"Remote server '{server_name}' not found in config")
            return None

        base_url = server_config.get("base_url", "").rstrip("/")
        api_key = server_config.get("api_key", "")

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "query": query,
            "top_k_fusion": top_k_fusion,
            "top_m_final": top_m_final
        }
        if kb_names:
            payload["kb_names"] = kb_names

        try:
            async with session.post(
                f"{base_url}/api/retrieve",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 401:
                    logger.error(f"Authentication failed for server '{server_name}'")
                    return None
                else:
                    text = await resp.text()
                    logger.error(f"Error from server '{server_name}': {resp.status} - {text}")
                    return None

        except aiohttp.ClientError as e:
            logger.error(f"Connection error to server '{server_name}': {e}")
            return None

    async def _list_remote_kbs(self, server_name: str) -> Optional[list]:
        """获取远程服务器的知识库列表"""
        session = await self._ensure_client_session()

        server_config = self._get_server_config(server_name)
        if not server_config:
            return None

        base_url = server_config.get("base_url", "").rstrip("/")
        api_key = server_config.get("api_key", "")

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with session.get(
                f"{base_url}/api/kbs",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("knowledge_bases", [])
                return None

        except aiohttp.ClientError as e:
            logger.error(f"Error listing remote KBs: {e}")
            return None

    # ==================== Agent LLM 工具 ====================

    def _get_tool_description(self) -> str:
        """获取工具描述，包含配置的远程服务器信息"""
        servers = self._get_remote_servers()

        if not servers:
            return (
                "Query the remote knowledge base for facts or relevant context. "
                "NOTE: No remote servers are currently configured. "
                "Please contact the administrator to configure remote knowledge base servers."
            )

        # 构建服务器描述
        server_lines = []
        for server in servers:
            name = server.get("name", "unnamed")
            server_lines.append(f"- {name}")

        server_info = "\n".join(server_lines)

        return (
            "Query the remote knowledge base for facts or relevant context. "
            "Use this tool when the user's question requires information stored in remote knowledge bases. "
            f"Available remote servers:\n{server_info}\n\n"
            "IMPORTANT: You MUST specify the 'server_name' parameter to select which remote server to query."
        )

    @filter.llm_tool(name="astrbot_remote_kb_search")
    async def tool_remote_kb_search(self, event: AstrMessageEvent, server_name: str, query: str, kb_names: list[str] | str | None = None):
        '''Query the remote knowledge base for facts or relevant context.

        Args:
            server_name(string): The name of the remote server to query. Check get_remote_kb_servers() for available options.
            query(string): A concise keyword query for the knowledge base. Send short keywords or a concise question.
            kb_names(array[string]): Optional. Knowledge base names to search on the remote server. If empty, searches all accessible knowledge bases.
        '''
        # 解析 kb_names 参数（支持列表或逗号分隔字符串）
        parsed_kb_names = None
        if kb_names:
            if isinstance(kb_names, list):
                parsed_kb_names = kb_names
            elif isinstance(kb_names, str) and kb_names.strip():
                parsed_kb_names = [kb.strip() for kb in kb_names.split(",") if kb.strip()]

        # 使用辅助方法获取默认参数
        default_top_k, default_top_m = self._get_default_retrieve_params()

        result = await self._query_remote_server(
            server_name=server_name,
            query=query,
            kb_names=parsed_kb_names,
            top_k_fusion=default_top_k,
            top_m_final=default_top_m
        )

        if result is None:
            return f"error: Failed to query remote server '{server_name}'. The server may be unavailable or authentication failed."

        context_text = result.get("context_text", "")
        if not context_text:
            return f"No relevant knowledge found in remote server '{server_name}'."

        return context_text

    @filter.llm_tool(name="get_remote_kb_servers")
    async def tool_get_remote_kb_servers(self, event: AstrMessageEvent):
        '''Get the list of configured remote knowledge base servers.

        Args:
            无参数
        '''
        servers = self._get_remote_servers()

        if not servers:
            return "No remote knowledge base servers are currently configured."

        lines = ["已配置的远程知识库服务器:\n"]

        for i, server in enumerate(servers, 1):
            name = server.get("name", "unnamed")
            base_url = server.get("base_url", "")

            lines.append(f"{i}. {name}")
            lines.append(f"   地址: {base_url}")

            # 尝试获取远程服务器的知识库列表
            kb_info = await self._list_remote_kbs(name)
            if kb_info is not None:
                if kb_info:
                    kb_list = [kb.get("kb_name", "") for kb in kb_info]
                    lines.append(f"   知识库: {', '.join(kb_list)}")
                else:
                    lines.append(f"   知识库: (无)")
            else:
                lines.append(f"   知识库: (无法获取)")

        return "\n".join(lines)

    # ==================== 聊天指令接口 ====================

    @filter.command("remote_kb_list")
    async def cmd_list_remote_kbs(self, event: AstrMessageEvent):
        """列出已配置的远程知识库服务器"""
        servers = self._get_remote_servers()
        if not servers:
            yield event.plain_result("未配置任何远程知识库服务器。请在插件配置中添加服务器信息。")
            return

        lines = ["📡 已配置的远程知识库服务器:\n"]
        for i, server in enumerate(servers, 1):
            name = server.get("name", "unnamed")
            url = server.get("base_url", "unknown")
            lines.append(f"{i}. {name}")
            lines.append(f"   地址: {url}")

        yield event.plain_result("\n".join(lines))

    @filter.command("remote_kb_query")
    async def cmd_remote_query(self, event: AstrMessageEvent, server: str = "", query: str = ""):
        """查询远程知识库

        用法: /remote_kb_query <服务器名> <查询内容>
        """
        if not server or not query:
            yield event.plain_result("用法: /remote_kb_query <服务器名> <查询内容>\n例如: /remote_kb_query myserver 什么是AstrBot")
            return

        result = await self._query_remote_server(
            server_name=server,
            query=query,
            top_k_fusion=10,
            top_m_final=3
        )

        if not result:
            yield event.plain_result(f"查询失败: 无法连接到服务器 '{server}' 或认证失败")
            return

        context_text = result.get("context_text", "")

        if not context_text:
            yield event.plain_result(f"在服务器 '{server}' 的知识库中未找到相关内容。")
            return

        reply = f"🔍 查询结果 (来自 {server}):\n\n"
        reply += f"{context_text[:500]}"
        if len(context_text) > 500:
            reply += "\n...(内容已截断)"

        yield event.plain_result(reply)

    @filter.command("kb_servers_status")
    async def cmd_servers_status(self, event: AstrMessageEvent):
        """检查所有已配置远程服务器的健康状态"""
        servers = self._get_remote_servers()
        if not servers:
            yield event.plain_result("未配置任何远程知识库服务器。")
            return

        lines = ["🏥 远程服务器状态检查:\n"]

        # 复用已有的客户端会话
        session = await self._ensure_client_session()

        for server in servers:
            name = server.get("name", "unnamed")
            base_url = server.get("base_url", "").rstrip("/")
            api_key = server.get("api_key", "")

            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            try:
                async with session.get(
                    f"{base_url}/health",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        status = data.get("status", "unknown")
                        kb_status = data.get("knowledge_base", "unknown")
                        lines.append(f"✅ {name}: 正常 (KB: {kb_status})")
                    else:
                        lines.append(f"❌ {name}: HTTP {resp.status}")
            except Exception as e:
                lines.append(f"❌ {name}: 连接失败 - {str(e)[:50]}")

        yield event.plain_result("\n".join(lines))
