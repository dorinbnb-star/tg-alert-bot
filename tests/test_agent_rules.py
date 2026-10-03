from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED = """# Reguli de lucru pentru agenți
1. Orice task începe cu ANALIZĂ: citești codul, propui planul (fișiere, schimbări, riscuri, teste) și te oprești.
2. În faza de analiză nu modifici fișiere, nu creezi branch, nu faci commit și nu deschizi PR.
3. Implementezi numai după mesajul explicit "DA, implementează". "Ok" sau o întrebare nu înseamnă acord.
4. Dacă în timpul implementării apare ceva în afara planului aprobat, te oprești și întrebi.
5. Nu faci niciodată merge în main.
6. Nu modifici rules-v0.1.json, pragurile sau logica Telegram fără aprobare separată.
7. Nu declari nimic "gata" fără dovadă: output de test sau link de rulare.
"""


class AgentRulesTests(unittest.TestCase):
    def test_root_agents_md_is_exact(self) -> None:
        self.assertEqual(EXPECTED, (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
