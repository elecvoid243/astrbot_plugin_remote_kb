"""
AstrBot 远程知识库插件

将Astrbot的知识库查询功能包装为HTTP服务，支持跨Astrbot实例的知识库共享与查询。

功能说明:
- 服务端模式: 将本地知识库暴露为HTTP API，供其他Astrbot实例查询
- 客户端模式: 向远程Astrbot实例发起知识库查询请求
- Agent工具: 为Agent提供远程知识库查询能力

Author: elecvoid243
Created: 2026-04-15
Modified: 2026-04-22
"""

import asyncio
import json
import random
import traceback
from typing import Any, Optional, Union

import aiohttp
from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig


# ==================== 常量定义 ====================

PLUGIN_VERSION = "1.2.0"
PLUGIN_NAME = "astrbot_plugin_remote_kb"

# 支持的知识库文档类型（用于校验）
SUPPORTED_FILE_TYPES = {"txt", "pdf", "docx", "md", "html"}

# 默认请求超时（秒）
DEFAULT_TIMEOUT = 60
HEALTH_CHECK_TIMEOUT = 10
LIST_KBS_TIMEOUT = 30

# 客户端请求最大重试次数
MAX_RETRIES = 2

# 服务端请求体大小限制（50MB）
MAX_CLIENT_SIZE = 50 * 1024 * 1024


# ==================== 插件主体 ====================

@register(PLUGIN_NAME, "elecvoid243",
           "将Astrbot知识库查询功能包装为HTTP服务，支持跨Astrbot实例的知识库共享与查询。服务端和客户端都需要安装",
           PLUGIN_VERSION)
class RemoteKBSPlugin(Star):
    """
    远程知识库插件

    提供2种工作模式:
    1. Server Mode (服务端模式): 启动HTTP服务器，提供知识库查询API
    2. Client Mode (客户端模式): 向远程Astrbot实例查询知识库
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # HTTP服务相关
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self._server_task: Optional[asyncio.Task] = None

        # 知识库管理器
        self.kb_manager = None

        # 客户端会话
        self._client_session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        # 工具描述缓存
        self._cached_tool_description: Optional[str] = None

    async def initialize(self) -> None:
        """插件初始化"""
        logger.info("RemoteKB Plugin: 正在初始化...")

        # 获取知识库管理器
        self.kb_manager = self.context.kb_manager

        # 读取服务端配置
        server_settings = self.config.get("server_settings", {})
        if isinstance(server_settings, dict) and server_settings.get("enabled", True):
            self._server_task = asyncio.create_task(
                self._start_server(),
                name="remote_kb_server"
            )
            self._server_task.add_done_callback(self._on_server_task_done)
            host = server_settings.get("host", "0.0.0.0")
            port = server_settings.get("port", 8550)
            logger.info(f"RemoteKB Plugin: 服务端模式已启用，将监听 {host}:{port}")

        # 读取客户端配置
        client_settings = self.config.get("client_settings", {})
        if isinstance(client_settings, dict) and client_settings.get("enabled", False):
            logger.info("RemoteKB Plugin: 客户端模式已启用")
            await self._ensure_client_session()

        logger.info("RemoteKB Plugin: 初始化完成")

    async def terminate(self) -> None:
        """插件卸载/停用时的清理"""
        logger.info("RemoteKB Plugin: 正在停止...")

        # 取消服务端任务
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass

        # 清理HTTP服务
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        self.app = None
        self.site = None
        self.runner = None

        # 关闭客户端会话
        async with self._session_lock:
            if self._client_session and not self._client_session.closed:
                await self._client_session.close()
            self._client_session = None

        logger.info("RemoteKB Plugin: 已停止")

    def _on_server_task_done(self, task: asyncio.Task) -> None:
        """服务端任务完成回调，用于捕获异常"""
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("RemoteKB server task was cancelled")
        except Exception as e:
            logger.error(f"RemoteKB server task failed: {e}")

    # ==================== 配置提取辅助方法 ====================

    def _get_default_retrieve_params(self) -> tuple[int, int]:
        """获取检索默认参数"""
        client_settings = self.config.get("client_settings", {})
        if isinstance(client_settings, dict):
            return (
                client_settings.get("default_top_k", 10),
                client_settings.get("default_top_m", 5)
            )
        return (10, 5)

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
            middlewares=[
                self._logging_middleware,
                self._cors_middleware,
                self._auth_middleware
            ],
            client_max_size=MAX_CLIENT_SIZE
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

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port)

        try:
            await self.site.start()
        except OSError as e:
            if e.errno == 10048:  # Windows: WSAEADDRINUSE
                logger.error(f"[RemoteKB] Port {port} is already in use. Server failed to start.")
                raise RuntimeError(f"Port {port} is already in use") from e
            raise

        logger.info(f"RemoteKB HTTP Server started at http://{host}:{port}")

    @web.middleware
    async def _logging_middleware(self, request: web.Request, handler) -> web.Response:
        """请求日志中间件"""
        start_time = asyncio.get_event_loop().time()
        try:
            response = await handler(request)
            duration = (asyncio.get_event_loop().time() - start_time) * 1000
            logger.info(
                f"{request.remote} - {request.method} {request.path} "
                f"- {response.status} - {duration:.2f}ms"
            )
            return response
        except Exception as e:
            duration = (asyncio.get_event_loop().time() - start_time) * 1000
            logger.error(
                f"{request.remote} - {request.method} {request.path} "
                f"- ERROR - {duration:.2f}ms - {e}"
            )
            raise

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler) -> web.Response:
        """CORS中间件 - 统一处理跨域请求"""
        # 处理预检请求
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            response = await handler(request)

        if self.app and self.app.get("cors_enabled", True):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler) -> web.Response:
        """认证中间件 - 跳过OPTIONS预检请求"""
        # 跳过CORS预检请求，浏览器OPTIONS请求通常不携带Authorization头
        if request.method == "OPTIONS":
            return await handler(request)

        api_key = ""
        if self.app and "api_key" in self.app:
            api_key = self.app["api_key"]

        if not api_key:
            return await handler(request)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            # 使用 hmac.compare_digest 防止时序攻击
            import hmac
            if hmac.compare_digest(token, api_key):
                return await handler(request)

        return web.json_response(
            {"error": "Unauthorized", "message": "Invalid or missing API key"},
            status=401
        )

    def _json_error_response(self, status: int, message: str, log_error: str = None) -> web.Response:
        """返回统一的 JSON 错误响应，避免泄露敏感信息"""
        if log_error:
            logger.error(log_error)
        return web.json_response({"error": message}, status=status)

    async def _handle_index(self, request: web.Request) -> web.Response:
        """首页处理"""
        return web.json_response({
            "service": "AstrBot Remote KB Server",
            "version": PLUGIN_VERSION,
            "endpoints": {
                "health": "/health",
                "list_kbs": "/api/kbs",
                "retrieve": "/api/retrieve",
                "kb_info": "/api/kb/{kb_name}"
            }
        })

    async def _handle_health(self, request: web.Request) -> web.Response:
        """健康检查"""
        kb_status = "available" if self.kb_manager else "unavailable"
        return web.json_response({
            "status": "healthy",
            "knowledge_base": kb_status
        })

    async def _handle_list_kbs(self, request: web.Request) -> web.Response:
        """列出所有知识库"""
        try:
            if not self.kb_manager:
                return self._json_error_response(
                    503, "Knowledge base not available",
                    "Knowledge base manager not available"
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

            return web.json_response({"knowledge_bases": kb_list})

        except Exception as e:
            return self._json_error_response(
                500, "Internal Server Error",
                f"Error listing KBs: {e}\n{traceback.format_exc()}"
            )

    async def _handle_get_kb_info(self, request: web.Request) -> web.Response:
        """获取指定知识库的详细信息"""
        try:
            kb_name = request.match_info["kb_name"]

            if not self.kb_manager:
                return self._json_error_response(
                    503, "Knowledge base not available",
                    "Knowledge base manager not available"
                )

            kb_helper = await self.kb_manager.get_kb_by_name(kb_name)
            if not kb_helper:
                return self._json_error_response(
                    404, f"Knowledge base '{kb_name}' not found",
                    f"Knowledge base not found: {kb_name}"
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

            return web.json_response({
                "kb_id": kb.kb_id,
                "kb_name": kb.kb_name,
                "description": kb.description,
                "emoji": getattr(kb, 'emoji', '📚'),
                "doc_count": len(doc_list),
                "documents": doc_list
            })

        except Exception as e:
            return self._json_error_response(
                500, "Internal Server Error",
                f"Error getting KB info: {e}\n{traceback.format_exc()}"
            )

    async def _handle_retrieve(self, request: web.Request) -> web.Response:
        """知识库检索API"""
        try:
            data = await request.json()
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON from {request.remote}: {e}")
            return self._json_error_response(
                400, "Invalid JSON body",
                "Failed to parse JSON request body"
            )

        query = data.get("query", "")
        if not query:
            return self._json_error_response(
                400, "Missing required field: query",
                "Query field is missing"
            )

        # 使用辅助方法获取默认参数
        default_top_k, default_top_m = self._get_default_retrieve_params()

        kb_names = data.get("kb_names", [])
        top_k_fusion = data.get("top_k_fusion", default_top_k)
        top_m_final = data.get("top_m_final", default_top_m)

        # 获取允许的知识库列表
        allowed_kb_names = self.app.get("allowed_kb_names", []) if self.app else []
        # 确保 allowed_kb_names 是列表
        if not isinstance(allowed_kb_names, list):
            allowed_kb_names = []

        # 如果配置了 allowed_kb_names 且不为空，则应用过滤
        if allowed_kb_names:
            if kb_names:
                kb_names = [kb for kb in kb_names if kb in allowed_kb_names]
            else:
                kb_names = allowed_kb_names

            # 越权查询漏洞修复: 如果过滤后为空列表，说明请求的知识库都不在白名单中
            if not kb_names:
                return self._json_error_response(
                    403, "Access denied: requested knowledge base(s) are not in the allowed list",
                    f"Potential privilege bypass attempt - requested KBs not in allowed list"
                )

        try:
            if not self.kb_manager:
                return self._json_error_response(
                    503, "Knowledge base not available",
                    "Knowledge base manager not available"
                )

            result = await self.kb_manager.retrieve(
                query=query,
                kb_names=kb_names,
                top_k_fusion=top_k_fusion,
                top_m_final=top_m_final
            )

            if not result:
                result = {"context_text": "", "results": []}

            return web.json_response(result)

        except ValueError as e:
            logger.info(f"Validation error in retrieve: {e}")
            return self._json_error_response(400, str(e))
        except Exception as e:
            logger.exception(f"Unexpected error in retrieve: {e}")
            return self._json_error_response(
                500, "Internal Server Error",
                f"Error retrieving: {e}\n{traceback.format_exc()}"
            )

    # ==================== 客户端模式实现 ====================

    def _get_remote_servers(self) -> list:
        """获取远程服务器配置"""
        client_settings = self.config.get("client_settings", {})
        if isinstance(client_settings, dict):
            return client_settings.get("remote_servers", [])
        return []

    async def _ensure_client_session(self) -> aiohttp.ClientSession:
        """确保客户端会话已创建（线程安全）"""
        async with self._session_lock:
            if not self._client_session or self._client_session.closed:
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
        """向远程知识库服务器发起查询（带指数退避重试）"""
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

        last_exception = None
        for attempt in range(MAX_RETRIES + 1):
            session = await self._ensure_client_session()
            try:
                async with session.post(
                    f"{base_url}/api/retrieve",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
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
                last_exception = e
                if attempt < MAX_RETRIES:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        f"Request to '{server_name}' failed, retrying in {wait:.1f}s: {e}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        f"Request to '{server_name}' failed after {MAX_RETRIES + 1} attempts: {e}"
                    )

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
                timeout=aiohttp.ClientTimeout(total=LIST_KBS_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("knowledge_bases", [])
                return None

        except aiohttp.ClientError as e:
            logger.error(f"Error listing remote KBs from '{server_name}': {e}")
            return None

    # ==================== Agent LLM 工具 ====================

    def _get_tool_description(self) -> str:
        """获取工具描述，包含配置的远程服务器信息（带缓存）"""
        if self._cached_tool_description is not None:
            return self._cached_tool_description

        servers = self._get_remote_servers()

        if not servers:
            description = (
                "Query the remote knowledge base for facts or relevant context. "
                "NOTE: No remote servers are currently configured. "
                "Please contact the administrator to configure remote knowledge base servers."
            )
            self._cached_tool_description = description
            return description

        # 构建服务器描述
        server_lines = []
        for server in servers:
            name = server.get("name", "unnamed")
            server_lines.append(f"- {name}")

        server_info = "\n".join(server_lines)

        description = (
            "Query the remote knowledge base for facts or relevant context. "
            "Use this tool when the user's question requires information stored in remote knowledge bases. "
            f"Available remote servers:\n{server_info}\n\n"
            "IMPORTANT: You MUST specify the 'server_name' parameter to select which remote server to query."
        )
        self._cached_tool_description = description
        return description

    def _invalidate_tool_cache(self) -> None:
        """清除工具描述缓存（配置变更时调用）"""
        self._cached_tool_description = None

    @filter.llm_tool(name="astrbot_remote_kb_search")
    async def tool_remote_kb_search(self, event: AstrMessageEvent, server_name: str, query: str, kb_names: Optional[Union[list[str], str]] = None):
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
                    timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
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
