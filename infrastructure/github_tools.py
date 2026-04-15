"""GitHub Tools — 让 Agent 能访问 GitHub 提升开发能力。

通过 GitHub REST API / GraphQL API 让 Agent 搜索参考项目、
分析开源代码、获取最佳实践。

使用方法：
    from infrastructure.github_tools import GitHubTools
    tools = GitHubTools()  # 会自动读取 GITHUB_TOKEN 环境变量

    # Agent 可直接调用这些方法：
    tools.search_repos("FastAPI authentication best practices")
    tools.get_file("tiangolo/fastapi", "app/main.py")
    tools.search_code("JWT refresh token", language="python")
"""

from __future__ import annotations

import base64
import os
import textwrap
import time
from dataclasses import dataclass
from typing import Any

import requests

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

DEFAULT_TIMEOUT = 30


# ─── Rate Limit Helpers ────────────────────────────────────────────────────────

def _rate_limit_info(headers: dict) -> dict:
    remaining = headers.get("x-ratelimit-remaining", "N/A")
    reset = headers.get("x-ratelimit-reset", "N/A")
    return {"remaining": remaining, "reset": reset}


def _handle_rate_limit(headers: dict):
    """Check rate limit and sleep if needed."""
    remaining = int(headers.get("x-ratelimit-remaining", 9999))
    if remaining < 5:
        reset_ts = int(headers.get("x-ratelimit-reset", 0))
        import datetime
        wait = max(0, reset_ts - time.time()) + 2
        if wait > 0:
            import time as t
            print(f"[GitHub Tools] Rate limit low ({remaining}), waiting {wait:.0f}s...")
            t.sleep(wait)


def _request(method: str, url: str, token: str | None = None, **kwargs) -> requests.Response:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"

    resp = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT), **kwargs)
    resp.raise_for_status()
    _handle_rate_limit(resp.headers)
    return resp


# ─── Tool Definitions ─────────────────────────────────────────────────────────

@dataclass
class GitHubTool:
    """一个 GitHub 工具的定义（供 Agent 理解接口）。"""
    name: str
    description: str
    input_schema: dict[str, Any]
    fn_name: str  # 对应 GitHubTools 的方法名


TOOLS: list[GitHubTool] = [
    GitHubTool(
        name="github_search_repos",
        description="搜索 GitHub 仓库。适合找参考项目、学习最佳实践。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，如 'FastAPI authentication'"},
                "language": {"type": "string", "description": "语言过滤，如 'python', 'typescript'"},
                "sort": {"type": "string", "description": "排序：'stars', 'forks', 'updated'"},
                "per_page": {"type": "integer", "description": "返回数量，默认5"},
            },
            "required": ["query"],
        },
        fn_name="search_repos",
    ),
    GitHubTool(
        name="github_search_code",
        description="在代码中搜索。适合找具体实现方式、最佳实践代码片段。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询，如 'JWT token refresh in:file language:python'"},
                "per_page": {"type": "integer", "description": "返回数量，默认5"},
            },
            "required": ["query"],
        },
        fn_name="search_code",
    ),
    GitHubTool(
        name="github_get_file",
        description="读取仓库中的单个文件内容。适合分析具体实现。",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "仓库所有者，如 'tiangolo'"},
                "repo": {"type": "string", "description": "仓库名，如 'fastapi'"},
                "path": {"type": "string", "description": "文件路径，如 'app/main.py'"},
                "ref": {"type": "string", "description": "分支/标签/ commit，默认 'main'"},
            },
            "required": ["owner", "repo", "path"],
        },
        fn_name="get_file",
    ),
    GitHubTool(
        name="github_get_repo_info",
        description="获取仓库概览信息（stars、forks、语言、描述）。",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "仓库所有者"},
                "repo": {"type": "string", "description": "仓库名"},
            },
            "required": ["owner", "repo"],
        },
        fn_name="get_repo_info",
    ),
    GitHubTool(
        name="github_list_commits",
        description="获取仓库最近提交记录。适合了解项目的活跃度和开发节奏。",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "仓库所有者"},
                "repo": {"type": "string", "description": "仓库名"},
                "path": {"type": "string", "description": "只看某个文件/目录的提交历史"},
                "per_page": {"type": "integer", "description": "返回数量，默认10"},
            },
            "required": ["owner", "repo"],
        },
        fn_name="list_commits",
    ),
    GitHubTool(
        name="github_get_readme",
        description="获取仓库的 README 文件内容。适合快速了解项目用途和使用方式。",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "仓库所有者"},
                "repo": {"type": "string", "description": "仓库名"},
            },
            "required": ["owner", "repo"],
        },
        fn_name="get_readme",
    ),
    GitHubTool(
        name="github_list_issues",
        description="列出仓库的 Issues。适合了解项目的已知问题和社区讨论。",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "仓库所有者"},
                "repo": {"type": "string", "description": "仓库名"},
                "state": {"type": "string", "description": "'open', 'closed', 'all'"},
                "labels": {"type": "string", "description": "按标签过滤，如 'bug,help wanted'"},
                "per_page": {"type": "integer", "description": "返回数量，默认10"},
            },
            "required": ["owner", "repo"],
        },
        fn_name="list_issues",
    ),
    GitHubTool(
        name="github_analyze_repo_structure",
        description="分析仓库的目录结构。适合了解一个陌生项目的代码组织方式。",
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "仓库所有者"},
                "repo": {"type": "string", "description": "仓库名"},
                "path": {"type": "string", "description": "分析哪个目录，默认根目录"},
                "depth": {"type": "integer", "description": "递归深度，默认2"},
            },
            "required": ["owner", "repo"],
        },
        fn_name="analyze_repo_structure",
    ),
]


# ─── GitHub Tools Implementation ──────────────────────────────────────────────

class GitHubTools:
    """GitHub 工具集 — 提供 Agent 可调用的 GitHub 访问能力。

    所有方法都遵循相同的返回格式:
        {"ok": True, "data": ..., "meta": {"query": ..., "rate_limit": ...}}

    出错时返回:
        {"ok": False, "error": "message", "meta": {...}}
    """

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.session = requests.Session()
        self._setup_session()

    def _setup_session(self):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.session.headers.update(headers)

    def _get(self, url: str, **kwargs) -> dict:
        """统一 GET 请求，返回格式化结果。"""
        try:
            resp = self.session.get(url, timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT), **kwargs)
            resp.raise_for_status()
            _handle_rate_limit(resp.headers)
            return {
                "ok": True,
                "data": resp.json(),
                "meta": {
                    "status_code": resp.status_code,
                    "rate_limit": _rate_limit_info(resp.headers),
                    "url": url,
                },
            }
        except requests.HTTPError as e:
            return {"ok": False, "error": str(e), "meta": {"status_code": e.response.status_code}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _post(self, url: str, **kwargs) -> dict:
        """统一 POST 请求（用于 GraphQL）。"""
        try:
            resp = self.session.post(url, json=kwargs, timeout=kwargs.pop("timeout", DEFAULT_TIMEOUT))
            resp.raise_for_status()
            _handle_rate_limit(resp.headers)
            return {"ok": True, "data": resp.json(), "meta": {"status_code": resp.status_code}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ─── Public Tools ───────────────────────────────────────────────────────

    def search_repos(
        self,
        query: str,
        language: str | None = None,
        sort: str = "stars",
        per_page: int = 5,
    ) -> dict:
        """搜索 GitHub 仓库。

        Example:
            tools.search_repos("FastAPI authentication JWT", language="python", sort="stars")
        """
        q = query
        if language:
            q = f"{query} language:{language}"
        url = f"{GITHUB_API}/search/repositories?q={q}&sort={sort}&per_page={per_page}"
        result = self._get(url)

        if not result["ok"]:
            return result

        items = result["data"].get("items", [])
        return {
            **result,
            "data": [
                {
                    "name": r["full_name"],
                    "description": r.get("description"),
                    "stars": r["stargazers_count"],
                    "forks": r["forks_count"],
                    "language": r.get("language"),
                    "url": r["html_url"],
                    "pushed_at": r.get("pushed_at"),
                    "topics": r.get("topics", [])[:5],
                }
                for r in items
            ],
        }

    def search_code(
        self,
        query: str,
        per_page: int = 5,
    ) -> dict:
        """在代码中搜索。

        Example:
            tools.search_code("JWT refresh token in:file language:python size:>1000")
        """
        url = f"{GITHUB_API}/search/code?q={query}&per_page={per_page}"
        result = self._get(url)

        if not result["ok"]:
            return result

        items = result["data"].get("items", [])
        return {
            **result,
            "data": [
                {
                    "name": r["name"],
                    "repo": r["repository"]["full_name"],
                    "path": r["path"],
                    "url": r["html_url"],
                    "score": r.get("score"),
                }
                for r in items
            ],
        }

    def get_file(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str = "main",
    ) -> dict:
        """读取仓库中的文件内容（自动解码 base64）。

        Example:
            tools.get_file("tiangolo", "fastapi", "app/main.py")
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        result = self._get(url)

        if not result["ok"]:
            return result

        data = result["data"]
        if isinstance(data, dict) and data.get("encoding") == "base64":
            try:
                content = base64.b64decode(data["content"]).decode("utf-8")
                result["data"] = {
                    "path": data["path"],
                    "size": data.get("size", len(content)),
                    "sha": data["sha"],
                    "content": content,
                    "truncated": data.get("truncated", False),
                }
            except Exception as e:
                result["data"] = {"error": f"Decode failed: {e}"}

        return result

    def get_repo_info(self, owner: str, repo: str) -> dict:
        """获取仓库概览。

        Example:
            tools.get_repo_info("tiangolo", "fastapi")
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}"
        result = self._get(url)

        if not result["ok"]:
            return result

        r = result["data"]
        return {
            **result,
            "data": {
                "full_name": r["full_name"],
                "description": r.get("description"),
                "stars": r["stargazers_count"],
                "forks": r["forks_count"],
                "language": r.get("language"),
                "open_issues": r["open_issues_count"],
                "license": r.get("license", {}).get("name"),
                "topics": r.get("topics", [])[:10],
                "default_branch": r.get("default_branch"),
                "created_at": r.get("created_at"),
                "pushed_at": r.get("pushed_at"),
                "subscribers_count": r.get("subscribers_count"),
                "url": r["html_url"],
            },
        }

    def list_commits(
        self,
        owner: str,
        repo: str,
        path: str | None = None,
        per_page: int = 10,
    ) -> dict:
        """列出最近的提交。

        Example:
            tools.list_commits("tiangolo", "fastapi", per_page=5)
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/commits?per_page={per_page}"
        if path:
            url += f"&path={path}"
        result = self._get(url)

        if not result["ok"]:
            return result

        return {
            **result,
            "data": [
                {
                    "sha": c["sha"][:8],
                    "message": c["commit"]["message"].split("\n")[0],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                    "url": c["html_url"],
                }
                for c in result["data"]
            ],
        }

    def get_readme(self, owner: str, repo: str) -> dict:
        """获取 README 内容。

        Example:
            tools.get_readme("tiangolo", "fastapi")
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/readme"
        result = self._get(url)

        if not result["ok"]:
            return result

        data = result["data"]
        if isinstance(data, dict) and data.get("encoding") == "base64":
            try:
                content = base64.b64decode(data["content"]).decode("utf-8")
                result["data"] = {
                    "name": data["name"],
                    "content": content,
                    "size": data.get("size", len(content)),
                }
            except Exception as e:
                result["data"] = {"error": f"Decode failed: {e}"}

        return result

    def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        labels: str | None = None,
        per_page: int = 10,
    ) -> dict:
        """列出仓库的 Issues。

        Example:
            tools.list_issues("tiangolo", "fastapi", state="open", labels="good first issue")
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues?state={state}&per_page={per_page}"
        if labels:
            url += f"&labels={labels}"
        result = self._get(url)

        if not result["ok"]:
            return result

        return {
            **result,
            "data": [
                {
                    "number": i["number"],
                    "title": i["title"],
                    "state": i["state"],
                    "labels": [l["name"] for l in i.get("labels", [])],
                    "comments": i["comments"],
                    "url": i["html_url"],
                    "created_at": i["created_at"],
                }
                for i in result["data"] if not i.get("pull_request")
            ],
        }

    def analyze_repo_structure(
        self,
        owner: str,
        repo: str,
        path: str = "",
        depth: int = 2,
    ) -> dict:
        """分析仓库目录结构（递归获取）。

        Example:
            tools.analyze_repo_structure("tiangolo", "fastapi", path="app", depth=3)
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
        result = self._get(url)

        if not result["ok"]:
            return result

        data = result["data"]
        if not isinstance(data, list):
            return result

        def _extract_tree(items: list, current_depth: int) -> list[dict]:
            tree = []
            for item in items[:30]:  # 限制数量避免 API 超限
                entry = {
                    "name": item["name"],
                    "type": item["type"],  # "file" or "dir"
                    "size": item.get("size", 0),
                    "path": item["path"],
                }
                tree.append(entry)
            return tree

        result["data"] = _extract_tree(data, 0)
        return result

    # ─── Agent 友好封装 ──────────────────────────────────────────────────────

    def summarize_for_agent(self, result: dict, max_content_len: int = 3000) -> str:
        """将 GitHub API 结果格式化为 Agent 可读的文本。

        Args:
            result: GitHub API 返回的原始结果
            max_content_len: 文件内容最大长度（超过部分截断）
        """
        if not result.get("ok"):
            return f"❌ GitHub API 错误: {result.get('error', 'Unknown error')}"

        data = result["data"]

        # 单文件内容
        if isinstance(data, dict) and "content" in data:
            content = data["content"]
            if data.get("truncated") or len(content) > max_content_len:
                content = content[:max_content_len] + f"\n... (内容已截断, 原始大小: {data.get('size', 'N/A')} bytes)"
            return textwrap.dedent(f"""\
                📄 {data['path']}
                SHA: {data.get('sha', 'N/A')}

                {content}
            """)

        # 仓库信息
        if isinstance(data, dict) and "full_name" in data:
            return textwrap.dedent(f"""\
                📦 {data['full_name']}
                ⭐ {data.get('stars', 0):,} | 🍴 {data.get('forks', 0):,} | 💬 {data.get('open_issues', 0)}
                语言: {data.get('language', 'N/A')}
                描述: {data.get('description', 'N/A')}
                标签: {', '.join(data.get('topics', [])[:5]) or '无'}
                地址: {data.get('url', 'N/A')}
            """)

        # 仓库列表
        if isinstance(data, list) and data and "stars" in data[0]:
            lines = [f"🔍 找到 {len(data)} 个仓库:\n"]
            for r in data:
                lines.append(f"  ⭐ {r['stars']:,} {r['full_name']}")
                lines.append(f"     {r.get('description', 'N/A') or ''}")
                if r.get("topics"):
                    lines.append(f"     标签: {', '.join(r['topics'][:3])}")
            return "\n".join(lines)

        # 代码搜索结果
        if isinstance(data, list) and data and "repo" in data[0]:
            lines = [f"📂 找到 {len(data)} 个代码片段:\n"]
            for r in data:
                lines.append(f"  📄 {r['repo']} / {r['path']}")
                lines.append(f"     {r.get('url', '')}")
            return "\n".join(lines)

        # 提交记录
        if isinstance(data, list) and data and "sha" in data[0]:
            lines = [f"📜 最近 {len(data)} 次提交:\n"]
            for c in data:
                lines.append(f"  {c['sha']} — {c['message'][:60]} ({c['author']})")
            return "\n".join(lines)

        # Issues
        if isinstance(data, list) and data and "number" in data[0]:
            lines = [f"🐛 发现 {len(data)} 个 Issues:\n"]
            for i in data:
                labels = f"[{', '.join(i['labels'][:3])}]" if i["labels"] else ""
                lines.append(f"  #{i['number']} {labels} {i['title']}")
                lines.append(f"     {i['url']}")
            return "\n".join(lines)

        # 目录结构
        if isinstance(data, list) and data and "type" in data[0]:
            lines = [f"📁 目录结构 ({len(data)} 项):\n"]
            for item in data:
                icon = "📂" if item["type"] == "dir" else "📄"
                size = f" ({item['size']:,} B)" if item["type"] == "file" else ""
                lines.append(f"  {icon} {item['path']}{size}")
            return "\n".join(lines)

        # README
        if isinstance(data, dict) and "name" in data and data.get("name", "").lower().startswith("readme"):
            content = data["content"]
            if len(content) > max_content_len:
                content = content[:max_content_len] + "\n... (README 已截断)"
            return textwrap.dedent(f"""\
                📖 {data['name']}

                {content}
            """)

        # Fallback
        import json
        return json.dumps(data, indent=2, ensure_ascii=False)[:max_content_len]

    def call_tool(self, tool_name: str, **kwargs) -> dict:
        """根据工具名调用对应方法（供 ExecutionEngine 反射调用）。

        Example:
            result = tools.call_tool("github_search_repos", query="FastAPI JWT")
        """
        # 查找对应的方法
        method_map = {
            "github_search_repos": self.search_repos,
            "github_search_code": self.search_code,
            "github_get_file": self.get_file,
            "github_get_repo_info": self.get_repo_info,
            "github_list_commits": self.list_commits,
            "github_get_readme": self.get_readme,
            "github_list_issues": self.list_issues,
            "github_analyze_repo_structure": self.analyze_repo_structure,
        }

        method = method_map.get(tool_name)
        if not method:
            return {"ok": False, "error": f"Unknown tool: {tool_name}"}

        return method(**kwargs)
