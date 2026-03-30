"""
CR-152: Project Management Skill — task tracking, milestones, proactive follow-up.

Projects and tasks are stored as JSON in the agent's workspace (projects.json).
Context injection shows overdue tasks and upcoming deadlines in system prompt.
The orchestrator's daily wakeup triggers check_overdue automatically.

Tools:
  create_project(name, description, deadline)       — Create a new project
  add_task(project, title, assignee, deadline, notes) — Add task to a project
  update_task(project, title, status, notes)         — Update task status
  complete_task(project, title)                      — Mark task as done
  get_project_status(project)                        — Full project overview
  check_overdue()                                    — All overdue tasks across projects
  list_projects()                                    — Overview of all projects

Task statuses: open → in_progress → blocked → done
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("AIMOS.ProjectMgmt")

_VALID_STATUSES = {"open", "in_progress", "blocked", "done"}


class ProjectManagementSkill(BaseSkill):
    """Project and task tracking for agents — stored in workspace."""

    name = "project_management"
    display_name = "Project Management"

    def __init__(self, agent_name: str = "", **kwargs):
        self._agent_name = agent_name

    def is_available(self) -> bool:
        return bool(self._agent_name)

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "create_project",
                "description": (
                    "Create a new project to track tasks and milestones. "
                    "Use this when a user starts a multi-step initiative, "
                    "a request involves multiple people, or work spans several days."
                ),
                "parameters": {
                    "name": {"type": "string", "description": "Project name (unique identifier)", "required": True},
                    "description": {"type": "string", "description": "What is the goal of this project?", "default": ""},
                    "deadline": {"type": "string", "description": "Target completion date (YYYY-MM-DD), optional", "default": ""},
                },
            },
            {
                "name": "add_task",
                "description": (
                    "Add a task to an existing project. Tasks have an assignee "
                    "(person or agent responsible), a deadline, and a status. "
                    "Use this to break down projects into actionable steps."
                ),
                "parameters": {
                    "project": {"type": "string", "description": "Project name", "required": True},
                    "title": {"type": "string", "description": "Task title", "required": True},
                    "assignee": {"type": "string", "description": "Who is responsible (person name or agent name)", "default": ""},
                    "deadline": {"type": "string", "description": "Due date (YYYY-MM-DD), optional", "default": ""},
                    "notes": {"type": "string", "description": "Additional context or requirements", "default": ""},
                    "depends_on": {"type": "string", "description": "Title of task that must be done first, optional", "default": ""},
                },
            },
            {
                "name": "update_task",
                "description": (
                    "Update the status or notes of a task. "
                    "Valid statuses: open, in_progress, blocked, done."
                ),
                "parameters": {
                    "project": {"type": "string", "description": "Project name", "required": True},
                    "title": {"type": "string", "description": "Task title (or partial match)", "required": True},
                    "status": {"type": "string", "description": "New status: open, in_progress, blocked, done", "default": ""},
                    "notes": {"type": "string", "description": "Append note to task history", "default": ""},
                },
            },
            {
                "name": "complete_task",
                "description": "Mark a task as done.",
                "parameters": {
                    "project": {"type": "string", "description": "Project name", "required": True},
                    "title": {"type": "string", "description": "Task title (or partial match)", "required": True},
                },
            },
            {
                "name": "get_project_status",
                "description": (
                    "Show full status of a project: all tasks, progress, "
                    "overdue items, blocked items, and next actions."
                ),
                "parameters": {
                    "project": {"type": "string", "description": "Project name (or 'all' for overview)", "required": True},
                },
            },
            {
                "name": "check_overdue",
                "description": (
                    "Find all overdue or soon-due tasks across all projects. "
                    "Returns actionable summary for proactive follow-up. "
                    "Called automatically during daily review."
                ),
                "parameters": {
                    "days_warning": {"type": "integer", "description": "Warn N days before deadline (default: 2)", "default": 2},
                },
            },
            {
                "name": "list_projects",
                "description": "Show all projects with their progress summary.",
                "parameters": {},
            },
        ]

    # ── Storage ──────────────────────────────────────────────────────

    def _projects_path(self) -> Path:
        ws = self.workspace_path(self._agent_name)
        ws.mkdir(parents=True, exist_ok=True)
        return ws / "projects.json"

    def _load(self) -> dict:
        """Load projects dict. Structure: {project_name: {meta + tasks[]}}"""
        path = self._projects_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict):
        path = self._projects_path()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Tool dispatch ────────────────────────────────────────────────

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        dispatch = {
            "create_project": self._create_project,
            "add_task": self._add_task,
            "update_task": self._update_task,
            "complete_task": self._complete_task,
            "get_project_status": self._get_project_status,
            "check_overdue": self._check_overdue,
            "list_projects": self._list_projects,
        }
        handler = dispatch.get(tool_name)
        if not handler:
            return f"Unknown tool: {tool_name}"
        return handler(**{k: v for k, v in arguments.items() if v != ""})

    # ── Tool implementations ─────────────────────────────────────────

    def _create_project(self, name: str = "", description: str = "", deadline: str = "", **_) -> str:
        if not name:
            return "Error: 'name' is required."
        projects = self._load()
        key = name.strip().lower().replace(" ", "_")
        if key in projects:
            return f"Project '{name}' already exists. Use add_task to add tasks."

        project = {
            "name": name.strip(),
            "description": description.strip() if description else "",
            "deadline": deadline.strip() if deadline else None,
            "created": datetime.now(timezone.utc).isoformat(),
            "status": "active",
            "tasks": [],
        }

        if deadline:
            try:
                datetime.strptime(deadline.strip(), "%Y-%m-%d")
            except ValueError:
                return f"Error: Invalid deadline '{deadline}'. Use YYYY-MM-DD format."

        projects[key] = project
        self._save(projects)
        logger.info(f"[ProjectMgmt] {self._agent_name}: created project '{name}'")
        dl = f" (deadline: {deadline})" if deadline else ""
        return f"Project created: {name}{dl}"

    def _add_task(self, project: str = "", title: str = "", assignee: str = "",
                  deadline: str = "", notes: str = "", depends_on: str = "", **_) -> str:
        if not project or not title:
            return "Error: 'project' and 'title' are required."
        projects = self._load()
        key = project.strip().lower().replace(" ", "_")
        if key not in projects:
            return f"Project '{project}' not found. Use create_project first."

        if deadline:
            try:
                datetime.strptime(deadline.strip(), "%Y-%m-%d")
            except ValueError:
                return f"Error: Invalid deadline '{deadline}'. Use YYYY-MM-DD format."

        task = {
            "title": title.strip(),
            "assignee": assignee.strip() if assignee else None,
            "deadline": deadline.strip() if deadline else None,
            "status": "open",
            "notes": notes.strip() if notes else None,
            "depends_on": depends_on.strip() if depends_on else None,
            "history": [{"action": "created", "at": datetime.now(timezone.utc).isoformat()}],
        }
        projects[key]["tasks"].append(task)
        self._save(projects)
        logger.info(f"[ProjectMgmt] {self._agent_name}: added task '{title}' to '{project}'")
        parts = [f"Task added to {project}: {title}"]
        if assignee:
            parts.append(f"assigned to {assignee}")
        if deadline:
            parts.append(f"due {deadline}")
        if depends_on:
            parts.append(f"depends on '{depends_on}'")
        return " — ".join(parts)

    def _update_task(self, project: str = "", title: str = "", status: str = "",
                     notes: str = "", **_) -> str:
        if not project or not title:
            return "Error: 'project' and 'title' are required."
        if status and status not in _VALID_STATUSES:
            return f"Error: Invalid status '{status}'. Valid: {', '.join(sorted(_VALID_STATUSES))}"

        projects = self._load()
        key = project.strip().lower().replace(" ", "_")
        if key not in projects:
            return f"Project '{project}' not found."

        task = self._find_task(projects[key]["tasks"], title)
        if not task:
            return f"No task matching '{title}' found in project '{project}'."

        changes = []
        now = datetime.now(timezone.utc).isoformat()
        if status:
            old = task["status"]
            task["status"] = status
            task["history"].append({"action": f"status: {old} → {status}", "at": now})
            changes.append(f"status → {status}")
            if status == "done":
                task["completed"] = now
        if notes:
            if task.get("notes"):
                task["notes"] += f"\n[{now[:10]}] {notes}"
            else:
                task["notes"] = f"[{now[:10]}] {notes}"
            task["history"].append({"action": "note added", "at": now})
            changes.append("note added")

        if not changes:
            return "Nothing to update — provide 'status' or 'notes'."

        self._save(projects)
        return f"Task '{task['title']}' updated: {', '.join(changes)}"

    def _complete_task(self, project: str = "", title: str = "", **_) -> str:
        return self._update_task(project=project, title=title, status="done")

    def _get_project_status(self, project: str = "", **_) -> str:
        if not project:
            return "Error: 'project' is required."
        projects = self._load()

        if project.strip().lower() == "all":
            return self._list_projects()

        key = project.strip().lower().replace(" ", "_")
        if key not in projects:
            return f"Project '{project}' not found."

        p = projects[key]
        tasks = p["tasks"]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        total = len(tasks)
        done = sum(1 for t in tasks if t["status"] == "done")
        blocked = sum(1 for t in tasks if t["status"] == "blocked")
        overdue = sum(1 for t in tasks if t.get("deadline") and t["deadline"] < today and t["status"] != "done")

        lines = [
            f"PROJECT: {p['name']}",
            f"  {p.get('description', '')}",
            f"  Deadline: {p.get('deadline') or 'none'}",
            f"  Progress: {done}/{total} done" + (f", {blocked} blocked" if blocked else "") + (f", {overdue} overdue" if overdue else ""),
            "",
        ]

        # Group by status
        for status_label, status_key in [("OVERDUE", None), ("BLOCKED", "blocked"),
                                          ("IN PROGRESS", "in_progress"), ("OPEN", "open"), ("DONE", "done")]:
            if status_key is None:
                # Special: overdue
                group = [t for t in tasks if t.get("deadline") and t["deadline"] < today and t["status"] != "done"]
            else:
                group = [t for t in tasks if t["status"] == status_key]
                if status_key != "done":
                    # Remove overdue items from their normal group (already shown above)
                    group = [t for t in group if not (t.get("deadline") and t["deadline"] < today)]

            if not group:
                continue

            lines.append(f"  {status_label}:")
            for t in group:
                dl = f" [due {t['deadline']}]" if t.get("deadline") else ""
                assignee = f" → {t['assignee']}" if t.get("assignee") else ""
                dep = f" (needs: {t['depends_on']})" if t.get("depends_on") else ""
                lines.append(f"    {'[x]' if t['status'] == 'done' else '[ ]'} {t['title']}{dl}{assignee}{dep}")
                if t.get("notes") and t["status"] != "done":
                    # Show last note line only
                    last_note = t["notes"].strip().split("\n")[-1]
                    lines.append(f"        {last_note}")

        # Next actions suggestion
        actionable = [t for t in tasks if t["status"] in ("open", "in_progress") and t["status"] != "done"]
        if actionable:
            # Check dependencies
            done_titles = {t["title"].lower() for t in tasks if t["status"] == "done"}
            ready = []
            for t in actionable:
                dep = t.get("depends_on")
                if dep and dep.lower() not in done_titles:
                    continue
                ready.append(t)
            if ready:
                lines.append("")
                lines.append("  NEXT ACTIONS:")
                for t in ready[:5]:
                    assignee = f" → follow up with {t['assignee']}" if t.get("assignee") else ""
                    lines.append(f"    → {t['title']}{assignee}")

        return "\n".join(lines)

    def _check_overdue(self, days_warning: int = 2, **_) -> str:
        projects = self._load()
        if not projects:
            return "No projects found."

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        warning_date = (datetime.now(timezone.utc) + timedelta(days=days_warning)).strftime("%Y-%m-%d")

        overdue = []
        soon_due = []
        blocked = []

        for key, p in projects.items():
            if p.get("status") == "archived":
                continue
            pname = p["name"]

            # Project-level deadline
            if p.get("deadline") and p["deadline"] < today:
                done_count = sum(1 for t in p["tasks"] if t["status"] == "done")
                total = len(p["tasks"])
                if done_count < total:
                    overdue.append(f"  PROJECT '{pname}' deadline passed ({p['deadline']}) — {done_count}/{total} done")

            for t in p["tasks"]:
                if t["status"] == "done":
                    continue
                if t["status"] == "blocked":
                    blocked.append(f"  [{pname}] {t['title']}" + (f" → {t.get('assignee', '?')}" if t.get("assignee") else ""))
                if t.get("deadline"):
                    if t["deadline"] < today:
                        days_late = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(t["deadline"], "%Y-%m-%d")).days
                        overdue.append(
                            f"  [{pname}] {t['title']} — {days_late}d overdue"
                            + (f" → {t['assignee']}" if t.get("assignee") else "")
                        )
                    elif t["deadline"] <= warning_date:
                        soon_due.append(
                            f"  [{pname}] {t['title']} — due {t['deadline']}"
                            + (f" → {t['assignee']}" if t.get("assignee") else "")
                        )

        if not overdue and not soon_due and not blocked:
            return "All clear — no overdue tasks, nothing due soon, no blockers."

        lines = []
        if overdue:
            lines.append("OVERDUE:")
            lines.extend(overdue)
        if soon_due:
            lines.append("DUE SOON:")
            lines.extend(soon_due)
        if blocked:
            lines.append("BLOCKED:")
            lines.extend(blocked)

        return "\n".join(lines)

    def _list_projects(self, **_) -> str:
        projects = self._load()
        if not projects:
            return "No projects yet. Use create_project to start one."

        lines = ["PROJECTS:"]
        for key, p in projects.items():
            tasks = p["tasks"]
            total = len(tasks)
            done = sum(1 for t in tasks if t["status"] == "done")
            dl = f" (deadline: {p['deadline']})" if p.get("deadline") else ""
            status = "archived" if p.get("status") == "archived" else f"{done}/{total}"
            lines.append(f"  {p['name']}{dl} — {status}")

        return "\n".join(lines)

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _find_task(tasks: list[dict], title: str) -> dict | None:
        """Find task by exact or partial title match."""
        title_lower = title.strip().lower()
        # Exact match first
        for t in tasks:
            if t["title"].lower() == title_lower:
                return t
        # Partial match
        for t in tasks:
            if title_lower in t["title"].lower():
                return t
        return None


def get_project_context(agent_name: str) -> str:
    """Load overdue/upcoming tasks for injection into system prompt.

    Called at every think() cycle — lightweight JSON file read.
    Returns formatted string or empty string if nothing urgent.
    """
    try:
        ws = BaseSkill.workspace_path(agent_name)
        proj_path = ws / "projects.json"
        if not proj_path.exists():
            return ""

        projects = json.loads(proj_path.read_text(encoding="utf-8"))
        if not projects:
            return ""

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        week_ahead = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")

        lines = []
        for key, p in projects.items():
            if p.get("status") == "archived":
                continue
            pname = p["name"]
            for t in p["tasks"]:
                if t["status"] == "done":
                    continue
                dl = t.get("deadline")
                assignee = f" → {t['assignee']}" if t.get("assignee") else ""
                if t["status"] == "blocked":
                    lines.append(f"[BLOCKED] [{pname}] {t['title']}{assignee}")
                elif dl and dl < today:
                    lines.append(f"[OVERDUE] [{pname}] {t['title']}{assignee} (due {dl})")
                elif dl and dl == today:
                    lines.append(f"[TODAY] [{pname}] {t['title']}{assignee}")
                elif dl and dl == tomorrow:
                    lines.append(f"[TOMORROW] [{pname}] {t['title']}{assignee}")
                elif dl and dl <= week_ahead:
                    lines.append(f"[{dl}] [{pname}] {t['title']}{assignee}")

        if not lines:
            return ""
        return "\n<projects>\n" + "\n".join(lines) + "\n</projects>"

    except Exception:
        return ""
