"""
ETAMSSQLSkill – ETA Muhasebe entegrasyonu (Microsoft SQL Server).

MSSQL veritabanina baglanarak ETA muhasebe verilerine salt-okunur erisim saglar.
pymssql veya pyodbc Python kutuphanesi gerektirir.

Kurulum:
  pip install pymssql
  # veya: pip install pyodbc (ODBC driver gerektirir)
"""

import logging
import os

from .skill_eta_accounting import ETAAccountingBase, _CONNECT_TIMEOUT

logger = logging.getLogger("ETAMSSQLSkill")


class ETAMSSQLSkill(ETAAccountingBase):
    """ETA Muhasebe — Microsoft SQL Server veritabani baglantisi."""

    name = "eta_mssql"
    display_name = "ETA Accounting (MSSQL)"

    def _default_port(self) -> int:
        return 1433

    def _connect(self):
        """MSSQL veritabanina baglanir ve connection nesnesi dondurur."""
        # Oncelik: pymssql (daha kolay kurulur), yoksa pyodbc
        try:
            import pymssql
            _driver = "pymssql"
        except ImportError:
            try:
                import pyodbc
                _driver = "pyodbc"
            except ImportError:
                raise RuntimeError(
                    "MSSQL Python kutuphanesi bulunamadi. "
                    "Lutfen birini kurun: pip install pymssql (veya pip install pyodbc)"
                )

        try:
            if _driver == "pymssql":
                import pymssql
                conn = pymssql.connect(
                    server=self._host,
                    port=self._port,
                    database=self._db_name,
                    user=self._user,
                    password=self._password,
                    login_timeout=_CONNECT_TIMEOUT,
                    timeout=_CONNECT_TIMEOUT,
                    charset="UTF8",
                )
            else:
                import pyodbc
                conn_str = (
                    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                    f"SERVER={self._host},{self._port};"
                    f"DATABASE={self._db_name};"
                    f"UID={self._user};"
                    f"PWD={self._password};"
                    f"Connection Timeout={_CONNECT_TIMEOUT};"
                )
                conn = pyodbc.connect(conn_str, timeout=_CONNECT_TIMEOUT)
            return conn
        except Exception as exc:
            logger.error(f"MSSQL baglanti hatasi: {exc}")
            raise RuntimeError(
                f"MSSQL veritabanina baglanirken hata olustu ({self._host}:{self._port}): {exc}"
            )
