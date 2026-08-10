import sys
import os

# Adjust path to import backend modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules.reports.service import _normalize_list

def test_normalize():
    assert _normalize_list(None) == []
    assert _normalize_list("") == []
    assert _normalize_list("ME") == ["ME"]
    assert _normalize_list("ME,MAX") == ["ME", "MAX"]
    assert _normalize_list(["ME,MAX", "OTHER"]) == ["ME", "MAX", "OTHER"]
    assert _normalize_list(["  ME , MAX ", "   ", "OTHER"]) == ["ME", "MAX", "OTHER"]
    print("ALL NORMALIZATION TESTS PASSED!")

if __name__ == "__main__":
    test_normalize()
