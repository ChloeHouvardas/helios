"""The fact-key probe follows the helpers `extra_facts_for` calls."""
from __future__ import annotations

from pathlib import Path

from app_builder.validate import probe_fact_keys

SOURCE = '''
class Facts:
    def extra_facts_for(self, subject_id):
        facts = {"bill_amount_cents": 1}
        facts.update(self._supplier_facts(subject_id))
        helper_only(facts)
        return facts

    def _supplier_facts(self, supplier_id):
        return {"supplier_amount_ceiling_cents": 2, **self._deeper()}

    def _deeper(self):
        return {"from_a_second_hop": 3}

    def _never_called(self):
        return {"unreachable_key": 4}


def helper_only(facts):
    facts["module_level_key"] = 5
'''


def test_keys_from_called_helpers_count_as_produced(tmp_path: Path) -> None:
    facts = tmp_path / "facts.py"
    facts.write_text(SOURCE, encoding="utf-8")
    keys, methods, import_error = probe_fact_keys(facts)
    assert import_error is None
    assert {"bill_amount_cents", "supplier_amount_ceiling_cents",
            "from_a_second_hop", "module_level_key"} <= keys
    assert "unreachable_key" not in keys
    assert "extra_facts_for" in methods
