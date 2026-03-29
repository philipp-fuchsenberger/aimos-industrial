"""
ETAFirebirdSkill – ETA Muhasebe entegrasyonu (Firebird/InterBase).

Firebird veritabanina baglanarak ETA muhasebe verilerine salt-okunur erisim saglar.
fdb veya firebirdsql Python kutuphanesi gerektirir.

Kurulum:
  pip install firebirdsql
  # veya: pip install fdb (eski Firebird surumleri icin)
"""

import logging
import os

from .skill_eta_accounting import ETAAccountingBase, _CONNECT_TIMEOUT

logger = logging.getLogger("ETAFirebirdSkill")


class ETAFirebirdSkill(ETAAccountingBase):
    """ETA Muhasebe — Firebird/InterBase veritabani baglantisi."""

    name = "eta_firebird"
    display_name = "ETA Accounting (Firebird)"

    def _default_port(self) -> int:
        return 3050

    def _connect(self):
        """Firebird veritabanina baglanir ve connection nesnesi dondurur."""
        # Oncelik: firebirdsql (aktif surulmekte), yoksa fdb
        try:
            import firebirdsql
            _driver = "firebirdsql"
        except ImportError:
            try:
                import fdb
                _driver = "fdb"
            except ImportError:
                raise RuntimeError(
                    "Firebird Python kutuphanesi bulunamadi. "
                    "Lutfen birini kurun: pip install firebirdsql (veya pip install fdb)"
                )

        try:
            if _driver == "firebirdsql":
                import firebirdsql
                conn = firebirdsql.connect(
                    host=self._host,
                    port=self._port,
                    database=self._db_name,
                    user=self._user,
                    password=self._password,
                    timeout=_CONNECT_TIMEOUT,
                    charset="UTF8",
                )
            else:
                import fdb
                conn = fdb.connect(
                    host=self._host,
                    port=self._port,
                    database=self._db_name,
                    user=self._user,
                    password=self._password,
                    charset="UTF8",
                )
            return conn
        except Exception as exc:
            logger.error(f"Firebird baglanti hatasi: {exc}")
            raise RuntimeError(
                f"Firebird veritabanina baglanirken hata olustu ({self._host}:{self._port}): {exc}"
            )
