"""AgentScope-style Skill registry.

Newer AgentScope versions expose ``Toolkit.register_agent_skill``. The
installed project dependency may be older, so this module keeps the same
progressive-disclosure principle locally: expose only skill metadata first,
then expand selected SKILL.md files on demand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    body: str
    metadata: dict


class SkillRegistry:
    """Discover and expand SKILL.md directories."""

    def __init__(self, skills_root: Path) -> None:
        self.skills_root = skills_root
        self._skills = self._load_skills(skills_root)
        self._native_prompt = self._try_native_agent_scope_prompt()

    @property
    def skills(self) -> list[Skill]:
        return list(self._skills)

    def get_agent_skill_prompt(self) -> str:
        """Return the compact skill prompt shown to the model up front."""
        if self._native_prompt:
            return self._native_prompt

        if not self._skills:
            return "当前没有可用 Skill。"

        lines = [
            "你可以使用以下 Agent Skills。先根据 name/description 判断需要哪些 Skill；",
            "只有在确定需要时，后续上下文才会展开对应 SKILL.md 的详细流程。",
        ]
        for skill in self._skills:
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)

    def _try_native_agent_scope_prompt(self) -> Optional[str]:
        """Use AgentScope Toolkit skill registration when available."""
        try:
            from agentscope.tool import Toolkit  # type: ignore
        except Exception:
            return None

        toolkit = Toolkit()
        for skill in self._skills:
            try:
                toolkit.register_agent_skill(str(skill.path))
            except Exception:
                return None

        try:
            return toolkit.get_agent_skill_prompt()
        except Exception:
            return None

    def select_skills(self, maintenance_type: str, user_text: str) -> list[Skill]:
        """Select likely skills with deterministic routing.

        This keeps routing predictable for an API service. The prompt still
        receives the compact skill list, so LLM behavior follows the same
        selection-and-expansion model described in AgentScope's Skill guide.
        """
        query = f"{maintenance_type}\n{user_text}".lower()
        selected = []

        common = self.get("maintenance-plan-composer")
        if common:
            selected.append(common)

        route_rules = [
            (
                "ecs-lifecycle-maintenance",
                ["ecs", "云服务器", "创建ecs", "创建 ecs", "回收ecs", "回收 ecs", "ecs升配", "ecs 升配", "ecs降配", "维护性重启"],
            ),
            (
                "k8s-worker-maintenance",
                ["k8s", "kubernetes", "worker", "edas", "oam", "集群", "节点"],
            ),
            ("mq-maintenance-plan", ["mq", "rocketmq", "groupid", "gid", "topic", "队列"]),
            ("oss-maintenance-plan", ["oss", "bucket", "对象存储"]),
            ("polardb-maintenance-plan", ["polardb", "只读实例", "读写分离"]),
            ("rds-maintenance-plan", ["rds", "drds", "mysql", "参数调整"]),
            ("redis-maintenance-plan", ["redis", "缓存"]),
            ("slb-maintenance-plan", ["slb", "负载均衡", "监听", "后端服务器组", "ipv6"]),
            ("database-maintenance-plan", ["polardb", "mongodb", "mysql", "数据库", "白名单"]),
            ("component-scaling-plan", ["扩容", "缩容", "扩缩容", "规格", "变更配置"]),
            ("restart-maintenance-plan", ["重启", "维护性重启", "k8s", "节点"]),
        ]

        for skill_name, keywords in route_rules:
            if any(keyword.lower() in query for keyword in keywords):
                skill = self.get(skill_name)
                if skill and skill not in selected:
                    selected.append(skill)

        if len(selected) == 1:
            generic = self.get("generic-maintenance-plan")
            if generic:
                selected.append(generic)

        return selected

    def get(self, name: str) -> Optional[Skill]:
        for skill in self._skills:
            if skill.name == name:
                return skill
        return None

    def expand_skills(self, skills: Iterable[Skill]) -> str:
        blocks = []
        for skill in skills:
            blocks.append(
                f"## 已激活 Skill: {skill.name}\n"
                f"路径：{skill.path}\n\n"
                f"{skill.body.strip()}"
            )
        return "\n\n---\n\n".join(blocks)

    @staticmethod
    def _load_skills(skills_root: Path) -> list[Skill]:
        skills = []
        for skill_file in sorted(skills_root.glob("*/SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            match = FRONTMATTER_RE.match(text)
            metadata = {}
            body = text
            if match:
                metadata = yaml.safe_load(match.group(1)) or {}
                body = text[match.end() :]
            name = metadata.get("name") or skill_file.parent.name
            description = metadata.get("description", "")
            skills.append(
                Skill(
                    name=name,
                    description=description,
                    path=skill_file.parent,
                    body=body.strip(),
                    metadata=metadata,
                )
            )
        return skills
