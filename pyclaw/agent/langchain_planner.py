"""
Planner berbasis LangChain (opsional).

Memerlukan paket tambahan:
- langchain
- langchain-community
- langchain-ollama (opsional; fallback ke langchain_community.llms.Ollama)

Jika paket tidak terpasang, planner akan mengirim pesan error melalui `on_output`.
"""

from __future__ import annotations

from typing import Callable
from pathlib import Path


def plan_and_execute_lc(prompt: str, gateway, session, on_output: Callable[[str], None], model: str, system_text: str | None = None, temperature: float | None = None) -> None:
    try:
        try:
            from langchain_ollama import ChatOllama  # type: ignore
            llm = ChatOllama(model=model, temperature=temperature)
        except Exception:  # pragma: no cover - fallback import
            from langchain_community.llms import Ollama  # type: ignore
            llm = Ollama(model=model, temperature=temperature)

        try:
            from langchain.tools import BaseTool  # type: ignore
            from langchain.agents import AgentExecutor, create_react_agent  # type: ignore
            from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder  # type: ignore
        except Exception as e:  # pragma: no cover
            on_output(f"Error: Paket langchain belum lengkap: {e}")
            return

        class ShellTool(BaseTool):  # type: ignore
            name = "shell"
            description = "Execute shell command on user machine. Input is the full command string."

            def _run(self, command: str) -> str:  # type: ignore
                rc, out, err = gateway.channels["terminal"].send({"command": command, "cwd": str(session.cwd)})
                return f"$ {command}\nRC={rc}\nOUT=\n{out}\nERR=\n{err}"

            async def _arun(self, command: str) -> str:  # pragma: no cover
                return self._run(command)

        class SkillTool(BaseTool):  # type: ignore
            name = "skill"
            description = "Run a named Pyclaw skill by its JSON name (without .json). Input is the skill name."

            def _run(self, name: str) -> str:  # type: ignore
                result = gateway.run_skill(name)
                return f"[skill {name}] ok={result.get('ok')} log={result.get('log')}"

            async def _arun(self, name: str) -> str:  # pragma: no cover
                return self._run(name)

        class FileBase(BaseTool):  # type: ignore
            def _safe(self, p: str) -> Path:
                base = Path(session.cwd).resolve()
                target = (base / p).resolve()
                if base not in target.parents and target != base:
                    raise ValueError("path outside workspace is not allowed")
                return target

        class FileWriteTool(FileBase):  # type: ignore
            name = "file_write"
            description = "Write file relative to session cwd. Input: path|content separated by a newline with a blank line delimiter. Prefer JSON tool args if available."
            def _run(self, arg: str) -> str:  # type: ignore
                if "\n\n" in arg:
                    path_s, content = arg.split("\n\n", 1)
                else:
                    path_s, content = arg.split("\n", 1) if "\n" in arg else (arg, "")
                path = self._safe(path_s.strip())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                return f"[file_write] {path} bytes={path.stat().st_size}"

        class FileAppendTool(FileBase):  # type: ignore
            name = "file_append"
            description = "Append content to file relative to session cwd. Input same as file_write."
            def _run(self, arg: str) -> str:  # type: ignore
                if "\n\n" in arg:
                    path_s, content = arg.split("\n\n", 1)
                else:
                    path_s, content = arg.split("\n", 1) if "\n" in arg else (arg, "")
                path = self._safe(path_s.strip())
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(content)
                return f"[file_append] {path} bytes={path.stat().st_size}"

        class FileReadTool(FileBase):  # type: ignore
            name = "file_read"
            description = "Read file relative to session cwd. Input: path"
            def _run(self, arg: str) -> str:  # type: ignore
                path = self._safe(arg.strip())
                content = path.read_text() if path.exists() else ""
                return f"[file_read] {path}\n{content}"

        class FileListTool(FileBase):  # type: ignore
            name = "file_list"
            description = "List directory contents relative to session cwd. Input: path or '.'"
            def _run(self, arg: str) -> str:  # type: ignore
                path = self._safe(arg.strip() or ".")
                if path.is_dir():
                    items = "\n".join(sorted(p.name for p in path.iterdir()))
                else:
                    items = path.name
                return f"[file_list] {path}\n{items}"

        tools = []
        if session.allow_shell and "terminal" in gateway.channels:
            tools.append(ShellTool())
        tools.append(SkillTool())
        tools.extend([FileWriteTool(), FileAppendTool(), FileReadTool(), FileListTool()])

        # Siapkan prompt ReAct sederhana tanpa dependensi hub
        sys_txt = system_text or "Anda adalah asisten yang dapat menggunakan tools untuk menyelesaikan tugas. Gunakan tools bila relevan dan berikan jawaban ringkas."
        prompt_t = ChatPromptTemplate.from_messages([
            ("system", sys_txt),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        agent = create_react_agent(llm, tools, prompt_t)
        executor = AgentExecutor(agent=agent, tools=tools, verbose=False)  # type: ignore
        result = executor.invoke({"input": prompt})  # type: ignore
        text = result.get("output") if isinstance(result, dict) else str(result)
        on_output(text)
    except Exception as e:
        on_output(f"Error: {e}")
