"""
core/skills – AIMOS modular skill system v4.1.0.

SKILL_REGISTRY maps skill names → classes.
Only imports skills whose dependencies are available.
"""

from .base import BaseSkill
from .brave_search import BraveSearchSkill

SKILL_REGISTRY: dict[str, type[BaseSkill]] = {
    "brave_search": BraveSearchSkill,
}

# Conditional imports — don't crash if optional deps are missing
try:
    from .email_io import EmailSkill
    SKILL_REGISTRY["email"] = EmailSkill
except ImportError:
    pass

try:
    from .file_ops import FileOpsSkill
    SKILL_REGISTRY["file_ops"] = FileOpsSkill
except ImportError:
    pass

try:
    from .voice_io import VoiceIOSkill
    SKILL_REGISTRY["voice_io"] = VoiceIOSkill
except ImportError:
    pass

try:
    from .skill_shared_storage import SharedStorageSkill
    SKILL_REGISTRY["shared_storage"] = SharedStorageSkill
except ImportError:
    pass

try:
    from .skill_scheduler import SchedulerSkill
    SKILL_REGISTRY["scheduler"] = SchedulerSkill
except ImportError:
    pass

try:
    from .skill_hybrid_reasoning import HybridReasoningSkill
    SKILL_REGISTRY["hybrid_reasoning"] = HybridReasoningSkill
except ImportError:
    pass

try:
    from .skill_mail_monitor import MailMonitorSkill
    SKILL_REGISTRY["mail_monitor"] = MailMonitorSkill
except ImportError:
    pass

try:
    from .skill_web_automation import WebAutomationSkill
    SKILL_REGISTRY["web_automation"] = WebAutomationSkill
except ImportError:
    pass

try:
    from .skill_remote_storage import RemoteStorageSkill
    SKILL_REGISTRY["remote_storage"] = RemoteStorageSkill
except ImportError:
    pass

try:
    from .skill_football_observer import FootballObserverSkill
    SKILL_REGISTRY["football_observer"] = FootballObserverSkill
except ImportError:
    pass

try:
    from .skill_tr_calendar import TurkishCalendarSkill
    SKILL_REGISTRY["tr_calendar_awareness"] = TurkishCalendarSkill
except ImportError:
    pass

try:
    from .skill_persistence import PersistenceSkill
    SKILL_REGISTRY["persistence"] = PersistenceSkill
except ImportError:
    pass

try:
    from .skill_eta_firebird import ETAFirebirdSkill
    SKILL_REGISTRY["eta_firebird"] = ETAFirebirdSkill
except ImportError:
    pass

try:
    from .skill_eta_mssql import ETAMSSQLSkill
    SKILL_REGISTRY["eta_mssql"] = ETAMSSQLSkill
except ImportError:
    pass

try:
    from .skill_calendar import CalendarSkill
    SKILL_REGISTRY["calendar"] = CalendarSkill
except ImportError:
    pass

try:
    from .skill_contacts import ContactsSkill
    SKILL_REGISTRY["contacts"] = ContactsSkill
except ImportError:
    pass

try:
    from .skill_structural import StructuralSkill
    SKILL_REGISTRY["structural"] = StructuralSkill
except ImportError:
    pass

try:
    from .skill_project_management import ProjectManagementSkill
    SKILL_REGISTRY["project_management"] = ProjectManagementSkill
except ImportError:
    pass

try:
    from .skill_de_calendar import GermanCalendarSkill
    SKILL_REGISTRY["de_calendar_awareness"] = GermanCalendarSkill
except ImportError:
    pass

try:
    from .skill_document_ocr import DocumentOCRSkill
    SKILL_REGISTRY["document_ocr"] = DocumentOCRSkill
except ImportError:
    pass

__all__ = ["BaseSkill", "SKILL_REGISTRY"]
