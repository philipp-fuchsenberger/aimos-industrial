"""
CR-149: Structural Steel Engineering Skill

Provides structural analysis and design tools for steel structures.
Uses PyNite (FEM), ezdxf (DXF/CAD), and workspace-based databases
for profiles, regulations, and market prices.

Tools:
  analyze_beam(span, load, profile, supports)     — Quick beam analysis
  analyze_frame(config_file)                       — Full frame analysis from JSON config
  lookup_profile(name)                             — Steel profile properties
  update_profile_db()                              — Refresh profile database from web
  lookup_regulation(country, parameter)            — Building code parameters
  estimate_cost(profiles_json)                     — Cost estimation (₺/kg + weight)
  generate_dxf(config_file)                        — Generate DXF drawing
"""

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import BaseSkill

logger = logging.getLogger("AIMOS.Structural")

# Default Turkish steel profile database (IPE, HEA, HEB series)
# Properties: h(mm), b(mm), tw(mm), tf(mm), A(cm²), Iy(cm⁴), Iz(cm⁴), Wy(cm³), weight(kg/m)
_DEFAULT_PROFILES = {
    "IPE 80":  {"h":80,  "b":46,  "tw":3.8, "tf":5.2, "A":7.64,  "Iy":80.1,   "Iz":8.49,   "Wy":20.0,  "kg_m":6.0},
    "IPE 100": {"h":100, "b":55,  "tw":4.1, "tf":5.7, "A":10.3,  "Iy":171,    "Iz":15.9,   "Wy":34.2,  "kg_m":8.1},
    "IPE 120": {"h":120, "b":64,  "tw":4.4, "tf":6.3, "A":13.2,  "Iy":318,    "Iz":27.7,   "Wy":53.0,  "kg_m":10.4},
    "IPE 140": {"h":140, "b":73,  "tw":4.7, "tf":6.9, "A":16.4,  "Iy":541,    "Iz":44.9,   "Wy":77.3,  "kg_m":12.9},
    "IPE 160": {"h":160, "b":82,  "tw":5.0, "tf":7.4, "A":20.1,  "Iy":869,    "Iz":68.3,   "Wy":109,   "kg_m":15.8},
    "IPE 180": {"h":180, "b":91,  "tw":5.3, "tf":8.0, "A":23.9,  "Iy":1317,   "Iz":101,    "Wy":146,   "kg_m":18.8},
    "IPE 200": {"h":200, "b":100, "tw":5.6, "tf":8.5, "A":28.5,  "Iy":1943,   "Iz":142,    "Wy":194,   "kg_m":22.4},
    "IPE 220": {"h":220, "b":110, "tw":5.9, "tf":9.2, "A":33.4,  "Iy":2772,   "Iz":205,    "Wy":252,   "kg_m":26.2},
    "IPE 240": {"h":240, "b":120, "tw":6.2, "tf":9.8, "A":39.1,  "Iy":3892,   "Iz":284,    "Wy":324,   "kg_m":30.7},
    "IPE 270": {"h":270, "b":135, "tw":6.6, "tf":10.2,"A":45.9,  "Iy":5790,   "Iz":420,    "Wy":429,   "kg_m":36.1},
    "IPE 300": {"h":300, "b":150, "tw":7.1, "tf":10.7,"A":53.8,  "Iy":8356,   "Iz":604,    "Wy":557,   "kg_m":42.2},
    "IPE 330": {"h":330, "b":160, "tw":7.5, "tf":11.5,"A":62.6,  "Iy":11770,  "Iz":788,    "Wy":713,   "kg_m":49.1},
    "IPE 360": {"h":360, "b":170, "tw":8.0, "tf":12.7,"A":72.7,  "Iy":16270,  "Iz":1043,   "Wy":904,   "kg_m":57.1},
    "IPE 400": {"h":400, "b":180, "tw":8.6, "tf":13.5,"A":84.5,  "Iy":23130,  "Iz":1318,   "Wy":1156,  "kg_m":66.3},
    "IPE 450": {"h":450, "b":190, "tw":9.4, "tf":14.6,"A":98.8,  "Iy":33740,  "Iz":1676,   "Wy":1500,  "kg_m":77.6},
    "IPE 500": {"h":500, "b":200, "tw":10.2,"tf":16.0,"A":116,   "Iy":48200,  "Iz":2142,   "Wy":1928,  "kg_m":90.7},
    "IPE 550": {"h":550, "b":210, "tw":11.1,"tf":17.2,"A":134,   "Iy":67120,  "Iz":2668,   "Wy":2441,  "kg_m":106},
    "IPE 600": {"h":600, "b":220, "tw":12.0,"tf":19.0,"A":156,   "Iy":92080,  "Iz":3387,   "Wy":3069,  "kg_m":122},
    "HEA 100": {"h":96,  "b":100, "tw":5.0, "tf":8.0, "A":21.2,  "Iy":349,    "Iz":134,    "Wy":72.8,  "kg_m":16.7},
    "HEA 120": {"h":114, "b":120, "tw":5.0, "tf":8.0, "A":25.3,  "Iy":606,    "Iz":231,    "Wy":106,   "kg_m":19.9},
    "HEA 140": {"h":133, "b":140, "tw":5.5, "tf":8.5, "A":31.4,  "Iy":1033,   "Iz":389,    "Wy":155,   "kg_m":24.7},
    "HEA 160": {"h":152, "b":160, "tw":6.0, "tf":9.0, "A":38.8,  "Iy":1673,   "Iz":616,    "Wy":220,   "kg_m":30.4},
    "HEA 180": {"h":171, "b":180, "tw":6.0, "tf":9.5, "A":45.3,  "Iy":2510,   "Iz":925,    "Wy":294,   "kg_m":35.5},
    "HEA 200": {"h":190, "b":200, "tw":6.5, "tf":10.0,"A":53.8,  "Iy":3692,   "Iz":1336,   "Wy":389,   "kg_m":42.3},
    "HEA 220": {"h":210, "b":220, "tw":7.0, "tf":11.0,"A":64.3,  "Iy":5410,   "Iz":1955,   "Wy":515,   "kg_m":50.5},
    "HEA 240": {"h":230, "b":240, "tw":7.5, "tf":12.0,"A":76.8,  "Iy":7764,   "Iz":2769,   "Wy":675,   "kg_m":60.3},
    "HEA 260": {"h":250, "b":260, "tw":7.5, "tf":12.5,"A":86.8,  "Iy":10450,  "Iz":3668,   "Wy":836,   "kg_m":68.2},
    "HEA 280": {"h":270, "b":280, "tw":8.0, "tf":13.0,"A":97.3,  "Iy":13670,  "Iz":4763,   "Wy":1013,  "kg_m":76.4},
    "HEA 300": {"h":290, "b":300, "tw":8.5, "tf":14.0,"A":113,   "Iy":18260,  "Iz":6310,   "Wy":1260,  "kg_m":88.3},
    "HEB 100": {"h":100, "b":100, "tw":6.0, "tf":10.0,"A":26.0,  "Iy":450,    "Iz":167,    "Wy":89.9,  "kg_m":20.4},
    "HEB 120": {"h":120, "b":120, "tw":6.5, "tf":11.0,"A":34.0,  "Iy":864,    "Iz":318,    "Wy":144,   "kg_m":26.7},
    "HEB 140": {"h":140, "b":140, "tw":7.0, "tf":12.0,"A":43.0,  "Iy":1509,   "Iz":550,    "Wy":216,   "kg_m":33.7},
    "HEB 160": {"h":160, "b":160, "tw":8.0, "tf":13.0,"A":54.3,  "Iy":2492,   "Iz":889,    "Wy":311,   "kg_m":42.6},
    "HEB 180": {"h":180, "b":180, "tw":8.5, "tf":14.0,"A":65.3,  "Iy":3831,   "Iz":1363,   "Wy":426,   "kg_m":51.2},
    "HEB 200": {"h":200, "b":200, "tw":9.0, "tf":15.0,"A":78.1,  "Iy":5696,   "Iz":2003,   "Wy":570,   "kg_m":61.3},
    "HEB 220": {"h":220, "b":220, "tw":9.5, "tf":16.0,"A":91.0,  "Iy":8091,   "Iz":2843,   "Wy":736,   "kg_m":71.5},
    "HEB 240": {"h":240, "b":240, "tw":10.0,"tf":17.0,"A":106,   "Iy":11260,  "Iz":3923,   "Wy":938,   "kg_m":83.2},
    "HEB 260": {"h":260, "b":260, "tw":10.0,"tf":17.5,"A":118,   "Iy":14920,  "Iz":5135,   "Wy":1148,  "kg_m":93.0},
    "HEB 280": {"h":280, "b":280, "tw":10.5,"tf":18.0,"A":131,   "Iy":19270,  "Iz":6595,   "Wy":1376,  "kg_m":103},
    "HEB 300": {"h":300, "b":300, "tw":11.0,"tf":19.0,"A":149,   "Iy":25170,  "Iz":8563,   "Wy":1678,  "kg_m":117},
}

# Default Turkish market prices (₺/kg, approximate 2026)
_DEFAULT_PRICES = {
    "IPE": 28.5,
    "HEA": 30.0,
    "HEB": 31.5,
    "box": 33.0,
    "pipe": 35.0,
    "plate": 27.0,
}

# Seismic parameters for Turkish regions (TBDY 2018 simplified)
_TBDY_ZONES = {
    "istanbul": {"SDS": 0.776, "SD1": 0.336, "PGA": 0.40, "zone": "1"},
    "ankara":   {"SDS": 0.456, "SD1": 0.189, "PGA": 0.25, "zone": "2"},
    "izmir":    {"SDS": 0.624, "SD1": 0.298, "PGA": 0.35, "zone": "1"},
    "bursa":    {"SDS": 0.590, "SD1": 0.256, "PGA": 0.30, "zone": "1"},
    "antalya":  {"SDS": 0.380, "SD1": 0.156, "PGA": 0.20, "zone": "2"},
    "konya":    {"SDS": 0.250, "SD1": 0.098, "PGA": 0.15, "zone": "3"},
    "trabzon":  {"SDS": 0.350, "SD1": 0.145, "PGA": 0.18, "zone": "2"},
    "erzurum":  {"SDS": 0.678, "SD1": 0.312, "PGA": 0.38, "zone": "1"},
}


class StructuralSkill(BaseSkill):
    """Structural steel engineering — analysis, profiles, cost estimation."""

    name = "structural"
    display_name = "Structural Engineering (Steel)"

    def __init__(self, agent_name: str = "", **kwargs):
        self._agent_name = agent_name
        self._ensure_databases()

    def _ensure_databases(self):
        """Create default profile/price databases in workspace if they don't exist."""
        try:
            ws = self.workspace_path(self._agent_name)
            ws.mkdir(parents=True, exist_ok=True)

            profiles_path = ws / "steel_profiles.json"
            if not profiles_path.exists():
                profiles_path.write_text(
                    json.dumps(_DEFAULT_PROFILES, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info(f"[Structural] Created default profile DB for {self._agent_name}")

            prices_path = ws / "market_prices.json"
            if not prices_path.exists():
                prices_path.write_text(
                    json.dumps({
                        "prices_tl_per_kg": _DEFAULT_PRICES,
                        "updated": datetime.now(timezone.utc).isoformat(),
                        "source": "default estimates — update with update_profile_db()",
                    }, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

            regulations_path = ws / "regulations_tbdy2018.json"
            if not regulations_path.exists():
                regulations_path.write_text(
                    json.dumps({
                        "code": "TBDY 2018",
                        "country": "Turkey",
                        "seismic_zones": _TBDY_ZONES,
                        "load_factors": {
                            "dead": 1.4,
                            "live": 1.6,
                            "seismic": 1.0,
                            "wind": 1.6,
                            "snow": 1.6,
                        },
                        "combinations": [
                            "1.4D + 1.6L",
                            "1.2D + 1.0L + 1.0E",
                            "0.9D + 1.0E",
                            "1.2D + 1.6L + 0.5S",
                            "1.2D + 1.0L + 1.6W",
                        ],
                        "steel_fy": {"S235": 235, "S275": 275, "S355": 355},
                        "notes": "Simplified parameters. For detailed seismic analysis consult AFAD interactive map.",
                    }, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception as exc:
            logger.warning(f"[Structural] DB init failed: {exc}")

    def is_available(self) -> bool:
        return bool(self._agent_name)

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "analyze_beam",
                "description": (
                    "Analyze a simply supported or cantilever steel beam. "
                    "Returns: reactions, max moment, max shear, max deflection, utilization ratio. "
                    "Uses PyNite FEM engine."
                ),
                "parameters": {
                    "span_m": {"type": "number", "description": "Beam span in meters", "required": True},
                    "load_kn_m": {"type": "number", "description": "Uniform distributed load in kN/m", "required": True},
                    "profile": {"type": "string", "description": "Steel profile name, e.g. 'IPE 300'", "required": True},
                    "steel_grade": {"type": "string", "description": "Steel grade: S235, S275, or S355", "default": "S235"},
                    "support_type": {"type": "string", "description": "simply_supported or cantilever", "default": "simply_supported"},
                },
            },
            {
                "name": "lookup_profile",
                "description": (
                    "Look up steel profile properties (dimensions, area, moment of inertia, weight). "
                    "Search by exact name or partial match."
                ),
                "parameters": {
                    "name": {"type": "string", "description": "Profile name or search term, e.g. 'HEB 200' or 'IPE'", "required": True},
                },
            },
            {
                "name": "suggest_profile",
                "description": (
                    "Suggest the lightest adequate profile for a given moment and shear demand."
                ),
                "parameters": {
                    "required_moment_knm": {"type": "number", "description": "Required moment capacity in kN·m", "required": True},
                    "steel_grade": {"type": "string", "description": "S235, S275, or S355", "default": "S235"},
                    "series": {"type": "string", "description": "Profile series: IPE, HEA, or HEB", "default": "IPE"},
                },
            },
            {
                "name": "estimate_cost",
                "description": (
                    "Estimate material cost for a list of steel members. "
                    "Input: JSON string with members [{profile, length_m, count}]."
                ),
                "parameters": {
                    "members_json": {"type": "string", "description": "JSON array of members", "required": True},
                },
            },
            {
                "name": "lookup_regulation",
                "description": "Look up building code parameters (seismic, load factors, combinations).",
                "parameters": {
                    "country": {"type": "string", "description": "Country name or code (Turkey, Europe, USA)", "default": "Turkey"},
                    "city": {"type": "string", "description": "City for seismic parameters", "default": ""},
                    "parameter": {"type": "string", "description": "Specific parameter to look up", "default": ""},
                },
            },
            {
                "name": "update_profile_db",
                "description": (
                    "Update the steel profile database and market prices. "
                    "Uses web search to find current Turkish steel prices."
                ),
                "parameters": {},
            },
            {
                "name": "read_dxf",
                "description": (
                    "Read and analyze a DXF/AutoCAD file. Extracts: layers, text labels, "
                    "dimensions, line/polyline geometry, block references. "
                    "Use this to understand architectural drawings before designing."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "DXF filename in workspace or public/ folder", "required": True},
                },
            },
            {
                "name": "filter_dxf",
                "description": (
                    "Extract specific layers from a large DXF file into a smaller file. "
                    "Use this for big files (>10MB) — filter to only structural layers "
                    "before analyzing. Lists available layers if called without layer_names."
                ),
                "parameters": {
                    "input_file": {"type": "string", "description": "Source DXF filename", "required": True},
                    "output_file": {"type": "string", "description": "Output filename for filtered DXF", "required": True},
                    "layer_names": {"type": "string", "description": "Comma-separated layer names to keep (empty = list all layers)", "default": ""},
                },
            },
            {
                "name": "generate_dxf",
                "description": (
                    "Generate a DXF drawing file. Creates structural plans with grid lines, "
                    "column positions, beam layouts, profile labels, and dimensions. "
                    "Saves to workspace — use send_telegram_file to send to user."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "Output filename (e.g. 'plan_project_X.dxf')", "required": True},
                    "members_json": {"type": "string", "description": "JSON: {grids:[{x,y,label}], members:[{start,end,profile,type}], title, scale}", "required": True},
                },
            },
        ]

    def _load_profiles(self) -> dict:
        try:
            ws = self.workspace_path(self._agent_name)
            return json.loads((ws / "steel_profiles.json").read_text(encoding="utf-8"))
        except Exception:
            return _DEFAULT_PROFILES

    def _load_prices(self) -> dict:
        try:
            ws = self.workspace_path(self._agent_name)
            data = json.loads((ws / "market_prices.json").read_text(encoding="utf-8"))
            return data.get("prices_tl_per_kg", _DEFAULT_PRICES)
        except Exception:
            return _DEFAULT_PRICES

    @staticmethod
    def _safe_read_updated(prices_path) -> str:
        """CR-195: Safely read 'updated' field from prices JSON with error handling."""
        if not prices_path.exists():
            return "never"
        try:
            data = json.loads(prices_path.read_text(encoding="utf-8"))
            return data.get("updated", "unknown")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Failed to read prices file: {exc}")
            return "unknown (read error)"

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "analyze_beam":
            return self._analyze_beam(arguments)
        elif tool_name == "lookup_profile":
            return self._lookup_profile(arguments.get("name", ""))
        elif tool_name == "suggest_profile":
            return self._suggest_profile(arguments)
        elif tool_name == "estimate_cost":
            return self._estimate_cost(arguments.get("members_json", ""))
        elif tool_name == "lookup_regulation":
            return self._lookup_regulation(arguments)
        elif tool_name == "update_profile_db":
            return self._update_profile_db()
        elif tool_name == "read_dxf":
            return self._read_dxf(arguments.get("filename", ""))
        elif tool_name == "filter_dxf":
            return self._filter_dxf(arguments.get("input_file", ""), arguments.get("output_file", ""), arguments.get("layer_names", ""))
        elif tool_name == "generate_dxf":
            return self._generate_dxf(arguments.get("filename", ""), arguments.get("members_json", ""))
        return f"Unknown tool: {tool_name}"

    def _analyze_beam(self, args: dict) -> str:
        try:
            from Pynite import FEModel3D
        except ImportError:
            return "Error: PyNite not installed. Run: pip install PyNiteFEA"

        span = float(args.get("span_m", 0))
        load = float(args.get("load_kn_m", 0))
        profile_name = args.get("profile", "IPE 200").strip().upper()
        grade = args.get("steel_grade", "S235")
        support = args.get("support_type", "simply_supported")

        if span <= 0 or load == 0:
            return "Error: span_m and load_kn_m must be positive."

        profiles = self._load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return f"Error: Profile '{profile_name}' not found. Use lookup_profile to search."

        fy = {"S235": 235, "S275": 275, "S355": 355}.get(grade, 235)  # MPa
        E = 210000  # MPa
        G = 81000   # MPa

        A = profile["A"] * 1e-4      # cm² → m²
        Iy = profile["Iy"] * 1e-8    # cm⁴ → m⁴
        Iz = profile["Iz"] * 1e-8    # cm⁴ → m⁴
        J = Iz * 0.5                  # Torsion approximation
        Wy = profile["Wy"] * 1e-6    # cm³ → m³

        # Build FEM model
        m = FEModel3D()
        m.add_node('A', 0, 0, 0)
        m.add_node('B', span, 0, 0)
        m.add_material('Steel', E * 1e3, 0.3, G * 1e3, 7850)  # kN/m² units
        m.add_section(profile_name, A, Iy, Iz, J)
        m.add_member('M1', 'A', 'B', 'Steel', profile_name)

        if support == "cantilever":
            m.def_support('A', True, True, True, True, True, True)
        else:
            m.def_support('A', True, True, True, True, True, True)
            m.def_support('B', False, True, True, False, False, False)

        m.add_member_dist_load('M1', 'Fy', -load, -load, 0, span, 'D')
        m.add_load_combo('LC1', {'D': 1.0})
        m.analyze()

        member = m.members['M1']
        max_M = abs(member.max_moment("Mz", "LC1"))
        max_V = abs(member.max_shear("Fy", "LC1"))
        max_d = abs(member.min_deflection("dy", "LC1"))

        # Capacity check
        M_capacity = fy * 1e3 * Wy  # kN·m (plastic moment approximation)
        util_ratio = max_M / M_capacity if M_capacity > 0 else 999

        # Deflection limit
        d_limit = span / 250 * 1000  # mm
        d_mm = max_d * 1000

        result = [
            f"=== Beam Analysis: {profile_name} ({grade}) ===",
            f"Span: {span:.1f} m | Load: {load:.1f} kN/m | Support: {support}",
            "",
            "Results:",
            f"  Max Moment:     {max_M:.1f} kN·m",
            f"  Max Shear:      {max_V:.1f} kN",
            f"  Max Deflection: {d_mm:.1f} mm (limit: {d_limit:.1f} mm = L/{250})",
            "",
            "Capacity Check:",
            f"  Moment Capacity: {M_capacity:.1f} kN·m ({grade}, Wy={profile['Wy']:.0f} cm³)",
            f"  Utilization:     {util_ratio:.1%}",
            f"  Status:          {'OK' if util_ratio <= 1.0 else 'OVERLOADED — increase profile!'}",
            f"  Deflection:      {'OK' if d_mm <= d_limit else 'EXCEEDS LIMIT'}",
            "",
            f"Profile: {profile_name} — {profile['kg_m']} kg/m, h={profile['h']}mm",
        ]

        if util_ratio > 0.9:
            result.append("\nWarning: Utilization > 90% — consider upgrading to next size.")

        logger.info(f"[Structural] Beam analysis: {profile_name}, M={max_M:.1f}, util={util_ratio:.1%}")
        return "\n".join(result)

    def _lookup_profile(self, name: str) -> str:
        profiles = self._load_profiles()
        name_upper = name.strip().upper()

        # Exact match
        if name_upper in profiles:
            p = profiles[name_upper]
            return (
                f"{name_upper}: h={p['h']}mm, b={p['b']}mm, tw={p['tw']}mm, tf={p['tf']}mm, "
                f"A={p['A']}cm², Iy={p['Iy']}cm⁴, Iz={p['Iz']}cm⁴, Wy={p['Wy']}cm³, "
                f"weight={p['kg_m']} kg/m"
            )

        # Partial match
        matches = {k: v for k, v in profiles.items() if name_upper in k}
        if matches:
            lines = [f"Profiles matching '{name}' ({len(matches)} found):"]
            for k, p in sorted(matches.items()):
                lines.append(f"  {k}: h={p['h']}mm, A={p['A']}cm², Iy={p['Iy']}cm⁴, {p['kg_m']} kg/m")
            return "\n".join(lines)

        return f"No profiles matching '{name}'. Available series: IPE, HEA, HEB"

    def _suggest_profile(self, args: dict) -> str:
        M_req = float(args.get("required_moment_knm", 0))
        grade = args.get("steel_grade", "S235")
        series = args.get("series", "IPE").upper()

        if M_req <= 0:
            return "Error: required_moment_knm must be positive."

        fy = {"S235": 235, "S275": 275, "S355": 355}.get(grade, 235)
        profiles = self._load_profiles()

        candidates = []
        for name, p in profiles.items():
            if not name.startswith(series):
                continue
            Wy = p["Wy"] * 1e-6  # cm³ → m³
            M_cap = fy * 1e3 * Wy  # kN·m
            if M_cap >= M_req:
                candidates.append((p["kg_m"], name, M_cap, p))

        if not candidates:
            return f"No {series} profile found for M={M_req} kN·m with {grade}. Try HEB series or higher grade."

        candidates.sort()  # Lightest first
        top3 = candidates[:3]

        lines = [f"Suggested profiles for M ≥ {M_req:.1f} kN·m ({grade}):"]
        for i, (kg, name, M_cap, p) in enumerate(top3):
            marker = "→ RECOMMENDED" if i == 0 else ""
            lines.append(f"  {name}: {kg} kg/m, M_cap={M_cap:.1f} kN·m, util={M_req/M_cap:.0%} {marker}")

        return "\n".join(lines)

    def _estimate_cost(self, members_json: str) -> str:
        try:
            members = json.loads(members_json)
        except json.JSONDecodeError:
            return "Error: Invalid JSON. Format: [{\"profile\":\"IPE 300\",\"length_m\":12,\"count\":4}]"

        profiles = self._load_profiles()
        prices = self._load_prices()

        lines = ["=== Cost Estimation ==="]
        total_weight = 0
        total_cost = 0

        for mem in members:
            name = mem.get("profile", "").strip().upper()
            length = float(mem.get("length_m", 0))
            count = int(mem.get("count", 1))

            profile = profiles.get(name)
            if not profile:
                lines.append(f"  {name}: NOT FOUND — skipped")
                continue

            kg_m = profile["kg_m"]
            weight = kg_m * length * count
            series = name.split()[0]
            price_per_kg = prices.get(series, 30.0)
            cost = weight * price_per_kg

            total_weight += weight
            total_cost += cost
            lines.append(f"  {count}x {name} @ {length}m: {weight:.0f} kg × {price_per_kg:.1f} ₺/kg = {cost:,.0f} ₺")

        lines.append("")
        lines.append(f"TOTAL: {total_weight:,.0f} kg = {total_weight/1000:.1f} tons")
        lines.append(f"TOTAL COST: {total_cost:,.0f} ₺ ({total_cost/total_weight:.1f} ₺/kg avg)")

        return "\n".join(lines)

    def _lookup_regulation(self, args: dict) -> str:
        country = args.get("country", "Turkey").strip().lower()
        city = args.get("city", "").strip().lower()
        param = args.get("parameter", "").strip().lower()

        if "turk" in country or "tr" == country:
            try:
                ws = self.workspace_path(self._agent_name)
                reg = json.loads((ws / "regulations_tbdy2018.json").read_text(encoding="utf-8"))
            except Exception:
                reg = {"code": "TBDY 2018", "seismic_zones": _TBDY_ZONES}

            if city and city in reg.get("seismic_zones", {}):
                zone = reg["seismic_zones"][city]
                return (
                    f"TBDY 2018 — {city.title()}:\n"
                    f"  SDS={zone['SDS']}, SD1={zone['SD1']}, PGA={zone['PGA']}g\n"
                    f"  Seismic Zone: {zone['zone']}\n"
                    f"  Load Combinations: {', '.join(reg.get('combinations', []))}\n"
                    f"  Steel Grades: {json.dumps(reg.get('steel_fy', {}))}"
                )

            return f"TBDY 2018 parameters:\n{json.dumps(reg, indent=2, ensure_ascii=False)}"

        return f"Regulation database for '{country}' not yet available. Currently supported: Turkey (TBDY 2018)."

    def _update_profile_db(self) -> str:
        """Mark the profile DB for update — agent should use web_search to find current prices."""
        ws = self.workspace_path(self._agent_name)
        prices_path = ws / "market_prices.json"

        return (
            "Profile database update procedure:\n"
            "1. Use web_search to find current Turkish steel prices (₺/kg)\n"
            "   Search: 'türkiye çelik profil fiyatları 2026 IPE HEA HEB ₺/kg'\n"
            "2. Update the prices by editing the file with write_file:\n"
            f"   write_file('market_prices.json', ...)\n"
            "3. Current price file location: workspace/market_prices.json\n"
            f"4. Last updated: {self._safe_read_updated(prices_path)}"
        )

    def _read_dxf(self, filename: str) -> str:
        """Read and analyze a DXF file — extract layers, geometry, text, dimensions."""
        if not filename:
            return "Error: 'filename' is required."
        try:
            import ezdxf
        except ImportError:
            return "Error: ezdxf not installed. Run: pip install ezdxf"

        filename = filename.strip().replace("..", "").lstrip("/")
        ws = self.workspace_path(self._agent_name)

        # Search in workspace, then public folders of other agents
        candidates = [ws / filename, ws / "public" / filename]
        # Also check incoming/ (Telegram downloads)
        candidates.append(ws / "incoming" / filename)

        path = None
        for c in candidates:
            if c.exists():
                path = c
                break

        if not path:
            return f"File '{filename}' not found. Searched: workspace, public/, incoming/"

        try:
            doc = ezdxf.readfile(str(path))
            msp = doc.modelspace()

            # Extract layers
            layers = sorted(set(e.dxf.layer for e in msp if hasattr(e.dxf, 'layer')))

            # Extract text entities
            texts = []
            for e in msp:
                if e.dxftype() in ('TEXT', 'MTEXT'):
                    t = e.dxf.text if hasattr(e.dxf, 'text') else str(e.text) if hasattr(e, 'text') else ""
                    if t and len(t.strip()) > 0:
                        pos = f"({e.dxf.insert.x:.0f},{e.dxf.insert.y:.0f})" if hasattr(e.dxf, 'insert') else ""
                        texts.append(f"{t.strip()} {pos}")

            # Extract dimensions
            dims = []
            for e in msp:
                if e.dxftype() == 'DIMENSION':
                    try:
                        val = e.dxf.actual_measurement if hasattr(e.dxf, 'actual_measurement') else "?"
                        dims.append(f"{val}")
                    except Exception:
                        pass

            # Count geometry by type
            type_counts = {}
            for e in msp:
                t = e.dxftype()
                type_counts[t] = type_counts.get(t, 0) + 1

            # Bounding box
            try:
                from ezdxf import bbox
                box = bbox.extents(msp)
                if box.has_data:
                    width = box.size.x
                    height = box.size.y
                    bb_info = f"Bounding box: {width:.0f} x {height:.0f} units"
                else:
                    bb_info = "Bounding box: unknown"
            except Exception:
                bb_info = "Bounding box: calculation failed"

            # Block references (structural elements)
            blocks = []
            for e in msp:
                if e.dxftype() == 'INSERT':
                    blocks.append(e.dxf.name)
            block_summary = {}
            for b in blocks:
                block_summary[b] = block_summary.get(b, 0) + 1

            lines = [
                f"=== DXF Analysis: {filename} ===",
                f"Format: {doc.dxfversion}",
                bb_info,
                "",
                f"Layers ({len(layers)}):",
            ]
            for l in layers[:20]:
                lines.append(f"  {l}")

            lines.append("\nGeometry:")
            for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {t}: {c}")

            if texts:
                lines.append(f"\nText Labels ({len(texts)}):")
                for t in texts[:30]:
                    lines.append(f"  {t}")

            if dims:
                lines.append(f"\nDimensions ({len(dims)}):")
                for d in dims[:20]:
                    lines.append(f"  {d}")

            if block_summary:
                lines.append(f"\nBlock References ({sum(block_summary.values())}):")
                for b, c in sorted(block_summary.items(), key=lambda x: -x[1])[:15]:
                    lines.append(f"  {b}: {c}x")

            logger.info(f"[Structural] DXF read: {filename} — {len(layers)} layers, {sum(type_counts.values())} entities")
            return "\n".join(lines)

        except Exception as exc:
            return f"Error reading DXF: {exc}"

    def _filter_dxf(self, input_file: str, output_file: str, layer_names: str) -> str:
        """Filter a DXF file to only keep specific layers. For large files."""
        try:
            import ezdxf
        except ImportError:
            return "Error: ezdxf not installed."

        if not input_file:
            return "Error: 'input_file' is required."

        input_file = input_file.strip().replace("..", "").lstrip("/")
        ws = self.workspace_path(self._agent_name)
        candidates = [ws / input_file, ws / "public" / input_file, ws / "incoming" / input_file]
        path = None
        for c in candidates:
            if c.exists():
                path = c
                break
        if not path:
            return f"File '{input_file}' not found."

        try:
            doc = ezdxf.readfile(str(path))
            msp = doc.modelspace()

            # Get all layer names
            all_layers = sorted(set(e.dxf.layer for e in msp if hasattr(e.dxf, 'layer')))

            if not layer_names:
                # Just list available layers with entity counts
                layer_counts = {}
                for e in msp:
                    l = e.dxf.layer if hasattr(e.dxf, 'layer') else '0'
                    layer_counts[l] = layer_counts.get(l, 0) + 1
                lines = [f"Available layers ({len(all_layers)}):"]
                for l in all_layers:
                    lines.append(f"  {l}: {layer_counts.get(l, 0)} entities")
                return "\n".join(lines)

            # Filter: keep only specified layers
            keep = {l.strip() for l in layer_names.split(",")}
            to_delete = []
            kept = 0
            for e in msp:
                layer = e.dxf.layer if hasattr(e.dxf, 'layer') else '0'
                if layer not in keep:
                    to_delete.append(e)
                else:
                    kept += 1

            for e in to_delete:
                msp.delete_entity(e)

            output_file = output_file.strip().replace("..", "").lstrip("/")
            out_path = ws / output_file
            out_path.parent.mkdir(parents=True, exist_ok=True)
            doc.saveas(str(out_path))

            size_mb = out_path.stat().st_size / (1024 * 1024)
            logger.info(f"[Structural] DXF filtered: {input_file} → {output_file} ({kept} entities, {size_mb:.1f} MB)")
            return (
                f"Filtered DXF saved: {output_file}\n"
                f"Kept {kept} entities from layers: {', '.join(keep)}\n"
                f"Removed {len(to_delete)} entities from other layers\n"
                f"File size: {size_mb:.1f} MB\n"
                f"Use read_dxf('{output_file}') to analyze the filtered file."
            )

        except Exception as exc:
            return f"Error filtering DXF: {exc}"

    def _generate_dxf(self, filename: str, members_json: str) -> str:
        """Generate a structural steel DXF drawing with grid, members, labels."""
        if not filename or not members_json:
            return "Error: 'filename' and 'members_json' are required."
        try:
            import ezdxf
            from ezdxf.enums import TextEntityAlignment
        except ImportError:
            return "Error: ezdxf not installed."

        try:
            config = json.loads(members_json)
        except json.JSONDecodeError:
            return ("Error: Invalid JSON. Format: "
                    '{\"title\":\"Project X\",\"grids\":[{\"x\":0,\"label\":\"A\"},{\"x\":6,\"label\":\"B\"}],'
                    '\"members\":[{\"start\":[0,0],\"end\":[6,0],\"profile\":\"IPE 300\",\"type\":\"beam\"}]}')

        try:
            doc = ezdxf.new('R2010')
            msp = doc.modelspace()

            # Setup layers
            doc.layers.add("GRID", color=8)        # gray
            doc.layers.add("COLUMNS", color=1)      # red
            doc.layers.add("BEAMS", color=5)         # blue
            doc.layers.add("BRACING", color=3)       # green
            doc.layers.add("LABELS", color=7)        # white
            doc.layers.add("DIMENSIONS", color=2)    # yellow
            doc.layers.add("TITLE", color=7)

            scale = config.get("scale", 1.0)
            title = config.get("title", "AIMOS Structural Drawing")

            # Draw grid lines
            grids = config.get("grids", [])
            for g in grids:
                x = g.get("x", 0) * scale
                y = g.get("y", 0) * scale
                label = g.get("label", "")
                axis = g.get("axis", "x")

                if axis == "x":
                    msp.add_line((x, -2 * scale), (x, 20 * scale), dxfattribs={"layer": "GRID", "linetype": "DASHED"})
                    msp.add_text(label, height=0.5 * scale, dxfattribs={"layer": "GRID"}).set_placement((x, -3 * scale), align=TextEntityAlignment.CENTER)
                else:
                    msp.add_line((-2 * scale, y), (30 * scale, y), dxfattribs={"layer": "GRID", "linetype": "DASHED"})
                    msp.add_text(label, height=0.5 * scale, dxfattribs={"layer": "GRID"}).set_placement((-3 * scale, y), align=TextEntityAlignment.CENTER)

            # Draw structural members
            members = config.get("members", [])
            profiles_db = self._load_profiles()

            for m in members:
                start = m.get("start", [0, 0])
                end = m.get("end", [0, 0])
                profile = m.get("profile", "")
                mtype = m.get("type", "beam").lower()

                sx, sy = start[0] * scale, start[1] * scale
                ex, ey = end[0] * scale, end[1] * scale

                layer = "BEAMS" if mtype == "beam" else "COLUMNS" if mtype == "column" else "BRACING"

                # Draw member as line
                msp.add_line((sx, sy), (ex, ey), dxfattribs={"layer": layer})

                # Profile label at midpoint
                mx, my = (sx + ex) / 2, (sy + ey) / 2
                msp.add_text(
                    profile, height=0.3 * scale,
                    dxfattribs={"layer": "LABELS"}
                ).set_placement((mx, my + 0.3 * scale), align=TextEntityAlignment.CENTER)

                # Length dimension
                length = math.sqrt((ex - sx) ** 2 + (ey - sy) ** 2) / scale
                if length > 0:
                    msp.add_text(
                        f"{length:.1f}m", height=0.2 * scale,
                        dxfattribs={"layer": "DIMENSIONS"}
                    ).set_placement((mx, my - 0.5 * scale), align=TextEntityAlignment.CENTER)

                # Column base plate symbol (small square)
                if mtype == "column":
                    size = 0.3 * scale
                    msp.add_lwpolyline(
                        [(sx - size, sy - size), (sx + size, sy - size),
                         (sx + size, sy + size), (sx - size, sy + size), (sx - size, sy - size)],
                        dxfattribs={"layer": "COLUMNS"}
                    )

            # Title block
            msp.add_text(
                title, height=0.8 * scale,
                dxfattribs={"layer": "TITLE"}
            ).set_placement((-2 * scale, -6 * scale))
            msp.add_text(
                f"Generated by AIMOS Muhendis | {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                height=0.3 * scale,
                dxfattribs={"layer": "TITLE"}
            ).set_placement((-2 * scale, -7.5 * scale))

            # Material take-off note
            if members:
                takeoff = []
                for m in members:
                    p = m.get("profile", "?")
                    l = math.sqrt((m["end"][0] - m["start"][0]) ** 2 + (m["end"][1] - m["start"][1]) ** 2)
                    takeoff.append(f"{p} L={l:.1f}m")
                msp.add_text(
                    "Material: " + ", ".join(takeoff[:5]),
                    height=0.25 * scale,
                    dxfattribs={"layer": "TITLE"}
                ).set_placement((-2 * scale, -9 * scale))

            # Save
            ws = self.workspace_path(self._agent_name)
            ws.mkdir(parents=True, exist_ok=True)
            out_path = ws / filename
            doc.saveas(str(out_path))

            # Also copy to public/ for other agents
            public_dir = ws / "public"
            public_dir.mkdir(exist_ok=True)
            doc.saveas(str(public_dir / filename))

            logger.info(f"[Structural] DXF generated: {filename} — {len(members)} members, {len(grids)} grids")
            return (
                f"DXF saved: {filename} ({len(members)} members, {len(grids)} grid lines)\n"
                f"Location: workspace/{filename} + public/{filename}\n"
                f"Use send_telegram_file to send to user."
            )

        except Exception as exc:
            return f"Error generating DXF: {exc}"
