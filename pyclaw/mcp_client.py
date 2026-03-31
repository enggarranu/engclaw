from __future__ import annotations

from typing import Any, Dict, List


class MCPClient:
    def __init__(self, cfg_mcp: Dict[str, Any]):
        self.cfg = cfg_mcp or {}

    def list_servers(self) -> List[Dict[str, Any]]:
        return list(self.cfg.get("servers") or [])

    def call(self, server: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "error": "mcp_call not implemented"}

    def resource_read(self, server: str, uri: str) -> Dict[str, Any]:
        return {"ok": False, "error": "mcp_resource_read not implemented"}

